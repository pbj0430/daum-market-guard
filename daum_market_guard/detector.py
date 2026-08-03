from __future__ import annotations

from collections import defaultdict

from .db import Database
from .hashing import hamming_hex
from .models import Assessment


def assess_post(db: Database, post_id: int, hamming_threshold: int) -> Assessment:
    post = db.get_post(post_id)
    current_images = db.get_post_images(post_id)
    prior_images = list(db.iter_prior_images(post_id))
    blacklisted = db.author_is_blacklisted(str(post["author_name"]), str(post["author_id"]))

    matched_image_ids: set[int] = set()
    matched_posts: dict[int, str] = {}
    same_author_posts: set[int] = set()
    different_author_posts: set[int] = set()
    reasons: list[str] = []

    for image in current_images:
        for prior in prior_images:
            exact = image["sha256"] and image["sha256"] == prior.sha256
            similar = (
                hamming_hex(str(image["dhash"]), prior.dhash) <= hamming_threshold
                or hamming_hex(str(image["ahash"]), prior.ahash) <= hamming_threshold
            )
            if not exact and not similar:
                continue
            matched_image_ids.add(int(image["id"]))
            matched_posts[prior.post_id] = prior.post_url
            if _same_author(
                str(post["author_name"]),
                str(post["author_id"]),
                prior.author_name,
                prior.author_id,
            ):
                same_author_posts.add(prior.post_id)
            else:
                different_author_posts.add(prior.post_id)

    duplicate_image_count = len(matched_image_ids)
    duplicate_post_count = len(matched_posts)
    same_author_count = len(same_author_posts)
    different_author_count = len(different_author_posts)

    score = 0
    if blacklisted:
        score = max(score, 95)
        reasons.append("글쓴이가 활성 블랙리스트에 있습니다.")
    if duplicate_image_count:
        if different_author_count:
            score = max(score, min(95, 45 + duplicate_image_count * 15 + different_author_count * 10))
            reasons.append(
                f"다른 글쓴이의 과거 게시글과 유사한 이미지 {duplicate_image_count}장이 발견되었습니다."
            )
        else:
            score = max(score, min(45, 15 + duplicate_image_count * 10))
            reasons.append(
                f"같은 글쓴이의 과거 게시글과 유사한 이미지 {duplicate_image_count}장이 발견되었습니다."
            )
    if duplicate_post_count >= 2 and different_author_count:
        score = min(99, score + 10)
        reasons.append("둘 이상의 과거 게시글과 이미지가 겹칩니다.")
    if current_images and duplicate_image_count == len(current_images) and different_author_count:
        score = min(99, score + 10)
        reasons.append("수집된 이미지 대부분이 과거 게시글과 겹칩니다.")
    if not reasons:
        reasons.append("현재 기준으로 과거 이미지 재사용 신호가 약합니다.")

    grouped_links = _stable_links(matched_posts.values())
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


def _same_author(
    current_name: str,
    current_id: str,
    prior_name: str,
    prior_id: str,
) -> bool:
    if current_id and prior_id and current_id == prior_id:
        return True
    return bool(current_name and prior_name and current_name == prior_name)


def _stable_links(links: object) -> list[str]:
    seen: dict[str, None] = {}
    for link in links:
        seen.setdefault(str(link), None)
    return list(seen.keys())
