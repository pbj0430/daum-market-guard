from __future__ import annotations

from typing import Any

from .db import Database
from .hashing import hamming_hex
from .models import Assessment


def assess_post(db: Database, post_id: int, hamming_threshold: int) -> Assessment:
    effective_hamming_threshold = min(hamming_threshold, 4)
    post = db.get_post(post_id)
    current_images = db.get_post_images(post_id)
    prior_images = list(db.iter_prior_images(post_id))
    blacklisted = db.author_is_blacklisted(str(post["author_name"]), str(post["author_id"]))

    matched_image_ids: set[int] = set()
    exact_matched_image_ids: set[int] = set()
    matched_posts: dict[int, str] = {}
    same_author_posts: set[int] = set()
    different_author_posts: set[int] = set()
    match_details: list[dict[str, Any]] = []
    reasons: list[str] = []

    for image in current_images:
        for prior in prior_images:
            dhash_distance = hamming_hex(str(image["dhash"]), prior.dhash)
            ahash_distance = hamming_hex(str(image["ahash"]), prior.ahash)
            exact = image["sha256"] and image["sha256"] == prior.sha256
            similar = (
                dhash_distance <= effective_hamming_threshold
                and ahash_distance <= effective_hamming_threshold
            )
            if not exact and not similar:
                continue
            matched_image_ids.add(int(image["id"]))
            if exact:
                exact_matched_image_ids.add(int(image["id"]))
            matched_posts[prior.post_id] = prior.post_url
            author_relation = _author_relation(
                str(post["author_name"]),
                str(post["author_id"]),
                prior.author_name,
                prior.author_id,
            )
            if author_relation == "same":
                same_author_posts.add(prior.post_id)
            elif author_relation == "different":
                different_author_posts.add(prior.post_id)
            match_details.append(
                {
                    "source_url": prior.post_url,
                    "source_post_key": prior.post_key,
                    "source_title": prior.title,
                    "source_author": prior.author_name or prior.author_id,
                    "current_image_id": int(image["id"]),
                    "source_image_id": prior.id,
                    "exact": bool(exact),
                    "dhash_distance": dhash_distance,
                    "ahash_distance": ahash_distance,
                    "author_relation": author_relation,
                }
            )

    duplicate_image_count = len(matched_image_ids)
    exact_duplicate_image_count = len(exact_matched_image_ids)
    duplicate_post_count = len(matched_posts)
    same_author_count = len(same_author_posts)
    different_author_count = len(different_author_posts)

    score = 0
    if blacklisted:
        score = max(score, 95)
        reasons.append("글쓴이가 활성 블랙리스트에 있습니다.")
    if duplicate_image_count:
        if different_author_count:
            if duplicate_image_count == 1 and duplicate_post_count == 1:
                score = max(score, 45 if exact_duplicate_image_count else 25)
            else:
                score = max(
                    score,
                    min(95, 45 + duplicate_image_count * 15 + different_author_count * 10),
                )
            reasons.append(
                f"다른 글쓴이의 과거 게시글과 유사한 이미지 {duplicate_image_count}장이 발견되었습니다."
            )
        else:
            reasons.append(
                f"같은 글쓴이의 과거 게시글과 유사한 이미지 {duplicate_image_count}장이 발견되었습니다."
            )
    if duplicate_post_count >= 2 and different_author_count:
        score = min(99, score + 10)
        reasons.append("둘 이상의 과거 게시글과 이미지가 겹칩니다.")
    if len(current_images) >= 2 and duplicate_image_count == len(current_images) and different_author_count:
        score = min(99, score + 10)
        reasons.append("수집된 이미지 대부분이 과거 게시글과 겹칩니다.")
    if not reasons:
        reasons.append("현재 기준으로 과거 이미지 재사용 신호가 약합니다.")

    grouped_links = _stable_match_details(match_details)
    return Assessment(
        post_id=post_id,
        post_key=str(post["post_key"]),
        score=int(score),
        duplicate_image_count=duplicate_image_count,
        duplicate_post_count=duplicate_post_count,
        same_author_duplicate_count=same_author_count,
        different_author_duplicate_count=different_author_count,
        source_links=grouped_links,
        reasons=reasons,
    )


def maybe_blacklist(db: Database, assessment: Assessment, threshold: int) -> None:
    if assessment.score < threshold or assessment.different_author_duplicate_count == 0:
        return
    post = db.get_post(assessment.post_id)
    if db.author_is_blacklisted(str(post["author_name"]), str(post["author_id"])):
        return
    db.add_blacklist(
        author_name=str(post["author_name"]),
        author_id=str(post["author_id"]),
        reason="자동 판정: 과거 다른 글쓴이 게시글 이미지 재사용 의심",
        source_post_id=assessment.post_id,
        score=assessment.score,
    )


def _author_relation(
    current_name: str,
    current_id: str,
    prior_name: str,
    prior_id: str,
) -> str:
    if current_id and prior_id and current_id == prior_id:
        return "same"
    if current_name and prior_name:
        return "same" if current_name == prior_name else "different"
    if current_id and prior_id:
        return "different"
    return "unknown"


def _stable_match_details(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[int, int]] = set()
    stable: list[dict[str, Any]] = []
    for match in matches:
        key = (int(match["current_image_id"]), int(match["source_image_id"]))
        if key in seen:
            continue
        seen.add(key)
        stable.append(match)
    return stable
