# 다음 중고장터 이미지 재사용 감시 서비스

Daum Cafe 중고장터 게시판을 로그인된 브라우저 세션으로 주기적으로 확인하고, 게시글 이미지의 해시를 SQLite에 저장한 뒤 과거 게시글 이미지와 유사도를 비교합니다.

기본값은 댓글을 달지 않는 `dry_run`입니다. 자동 댓글은 카페 규칙, 명예훼손 위험, 오탐 가능성이 있으므로 `config.toml`에서 명시적으로 켜야 합니다. 댓글 문구도 “사기 확정”이 아니라 “이미지 재사용 주의 신호”로 작성됩니다.

## 설치

### Git으로 가져오기

```bash
git clone <YOUR_REPOSITORY_URL> daum-market-guard
cd daum-market-guard
```

### Windows PC

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item config.example.toml config.toml
```

### Raspberry Pi 3B

64비트 Raspberry Pi OS 권장입니다. Pi 3B는 메모리가 작아서 Playwright 내장 Chromium보다 시스템 Chromium이 안정적일 수 있습니다.

간단 설치:

```bash
chmod +x scripts/bootstrap_pi.sh
./scripts/bootstrap_pi.sh
```

수동 설치:

```bash
sudo apt update
sudo apt install -y python3-full python3-venv chromium-browser
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp config.example.toml config.toml
```

시스템 Chromium을 쓸 경우 `config.toml`에 아래를 추가합니다.

```toml
browser_executable_path = "/usr/bin/chromium-browser"
```

## 사용

1. 로그인 세션 만들기

```bash
python -m daum_market_guard login --config config.toml
```

Raspberry Pi에서는 시스템 Python 대신 아래처럼 venv 실행 스크립트를 쓰세요.

```bash
./scripts/run.sh login --config config.toml
```

브라우저가 열리면 직접 아이디와 비밀번호를 입력하고 로그인합니다. 로그인 완료 후 터미널에서 Enter를 누르면 세션이 `browser-profile`에 저장됩니다.

2. 1회 수집 및 판정

```bash
python -m daum_market_guard scan --config config.toml
```

Raspberry Pi:

```bash
./scripts/run.sh scan --config config.toml
```

3. 계속 실행

```bash
python -m daum_market_guard daemon --config config.toml
```

Raspberry Pi:

```bash
./scripts/run.sh daemon --config config.toml
```

4. 의심 글 확인

```bash
python -m daum_market_guard suspects --config config.toml --min-score 70
```

5. 블랙리스트 관리

```bash
python -m daum_market_guard blacklist list --config config.toml
python -m daum_market_guard blacklist add --config config.toml --author-name "닉네임" --reason "운영자 확인"
python -m daum_market_guard blacklist remove --config config.toml --author-name "닉네임"
```

6. 설치 검증

```bash
python scripts/smoke_check.py
python -m unittest discover -s tests -v
```

## 자동 댓글

`config.toml`에서 아래처럼 바꿔야 실제 댓글을 시도합니다.

```toml
[comment]
enabled = true
mode = "post"
min_score = 85
```

처음에는 반드시 `mode = "dry_run"`으로 로그와 DB 기록만 확인하세요. Daum Cafe의 댓글 DOM이 계정/게시판 상태에 따라 달라질 수 있어, 실패하면 `[selectors]` 값을 조정해야 합니다.

## 라즈베리파이 systemd 예시

`/etc/systemd/system/daum-market-guard.service`:

```ini
[Unit]
Description=Daum market guard
After=network-online.target

[Service]
WorkingDirectory=/home/pi/daum-market-guard
ExecStart=/home/pi/daum-market-guard/.venv/bin/python -m daum_market_guard daemon --config /home/pi/daum-market-guard/config.toml
Restart=always
RestartSec=20
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now daum-market-guard
journalctl -u daum-market-guard -f
```
