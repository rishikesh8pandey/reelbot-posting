"""Runs on GitHub Actions at every posting slot: posts the oldest reel waiting in reels/ to
Instagram through the official API, then records the result and deletes that reel's files.

The laptop uploads each finished reel as three files: reels/<id>.mp4, reels/<id>.jpg (cover)
and reels/<id>.json (caption and topic), the .json last. Instagram downloads the video from
this repository's GitHub Pages address. Results go to posted/<id>.json or failed/<id>.json,
which the laptop reads back.

Never posts the same reel twice: before posting, the account's recent posts are checked for
the same caption, and each run posts at most one reel.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://graph.instagram.com/" + os.environ.get("IG_API_VERSION", "v25.0")
ROOT = Path(__file__).resolve().parent
PAGES = os.environ["PAGES_URL"].rstrip("/") + "/"
USER = os.environ["IG_USER_ID"]
SAFE_TAGS = 5          # what Instagram allows on accounts where it caps hashtags
TRIES = 3              # posting slots a reel is tried at before it is given up on
POLL_SECONDS, POLL_MINUTES = 20, 10
PAGES_WAIT_SECONDS, PAGES_POLL_SECONDS = 600, 30

session = requests.Session()
session.headers["Authorization"] = "Bearer " + os.environ["IG_ACCESS_TOKEN"]   # never in a URL


class Refused(Exception):
    pass


def call(method: str, path: str, **params) -> dict:
    url = f"{API}/{path}"
    try:
        if method == "GET":
            response = session.get(url, params=params, timeout=60)
        else:
            response = session.post(url, data=params, timeout=120)
    except requests.RequestException as err:
        raise Refused(f"network problem ({type(err).__name__})")
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or "error" in body:
        error = body.get("error") or {}
        raise Refused(f'{error.get("message") or response.text[:200]} (code {error.get("code")}, '
                      f'HTTP {response.status_code})')
    return body


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def hashtag_limit() -> int:
    try:
        return int(json.loads((ROOT / "state" / "hashtag_limit.json").read_text(encoding="utf-8"))["max"])
    except (OSError, ValueError, KeyError):
        return 30


def trim_hashtags(caption: str, keep: int) -> str:
    seen = 0

    def first_ones(match):
        nonlocal seen
        seen += 1
        return match.group(0) if seen <= keep else ""

    text = re.sub(r"#\w+", first_ones, caption)
    return "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()


def online(url: str) -> bool:
    """GitHub Pages publishes a new file a few minutes after it is uploaded."""
    try:
        return requests.head(url, timeout=30, allow_redirects=True).status_code == 200
    except requests.RequestException:
        return False


def wait_until_online(url: str) -> bool:
    """A run started by the upload itself waits the few minutes GitHub Pages takes to publish."""
    deadline = time.time() + PAGES_WAIT_SECONDS
    while not online(url):
        if time.time() >= deadline:
            return False
        print("Waiting for GitHub Pages to publish the video...", flush=True)
        time.sleep(PAGES_POLL_SECONDS)
    return True


def already_posted(caption: str) -> dict | None:
    recent = call("GET", f"{USER}/media", fields="id,caption,permalink,timestamp", limit=10).get("data", [])
    return next((item for item in recent if (item.get("caption") or "").strip() == caption.strip()), None)


def create(video: str, cover: str | None, reel: dict, caption: str) -> str:
    extra = {"cover_url": cover} if cover else {}
    if reel.get("topic"):
        extra["audio_name"] = reel["topic"]
    return call("POST", f"{USER}/media", media_type="REELS", video_url=video, caption=caption,
                share_to_feed="true", **extra)["id"]


def processed(container: str) -> str:
    deadline = time.time() + POLL_MINUTES * 60
    status = "IN_PROGRESS"
    while time.time() < deadline and status == "IN_PROGRESS":
        time.sleep(POLL_SECONDS)
        status = call("GET", container, fields="status_code").get("status_code", "")
        print("Instagram is processing the video:", status, flush=True)
    return status


def post(reel_id: str, reel: dict) -> str:
    video = PAGES + f"reels/{reel_id}.mp4"
    cover = PAGES + f"reels/{reel_id}.jpg" if (ROOT / "reels" / f"{reel_id}.jpg").exists() else None
    caption = trim_hashtags(reel["caption"], hashtag_limit())
    found = already_posted(caption)
    if found:
        print(f"{reel_id} was already on Instagram; recording it")
        return finish(reel_id, found["id"], found.get("permalink"))
    for attempt in (1, 2):         # a processing ERROR is usually a download that broke on the way
        try:
            container = create(video, cover, reel, caption)
        except Refused as err:
            if not re.search("hashtag", str(err), re.I):
                raise
            write(ROOT / "state" / "hashtag_limit.json", {"max": SAFE_TAGS, "reason": str(err)[:300]})
            caption = trim_hashtags(caption, SAFE_TAGS)
            container = create(video, cover, reel, caption)
        status = processed(container)
        if status == "FINISHED":
            media = call("POST", f"{USER}/media_publish", creation_id=container)["id"]
            permalink = call("GET", media, fields="permalink").get("permalink")
            return finish(reel_id, media, permalink)
        print(f"Instagram could not process {reel_id} ({status}), attempt {attempt}", flush=True)
    return give_up_later(reel_id, reel, f"Instagram could not process the video ({status})")


def finish(reel_id: str, media: str, permalink: str | None) -> str:
    write(ROOT / "posted" / f"{reel_id}.json", {"media_id": media, "permalink": permalink,
                                                 "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    for suffix in (".mp4", ".jpg", ".json"):
        (ROOT / "reels" / f"{reel_id}{suffix}").unlink(missing_ok=True)
    print(f"POSTED {reel_id}: {permalink}")
    return "posted"


def give_up_later(reel_id: str, reel: dict, reason: str) -> str:
    reel["tries"] = reel.get("tries", 0) + 1
    if reel["tries"] < TRIES:
        write(ROOT / "reels" / f"{reel_id}.json", reel)
        print(f"{reel_id}: {reason}; it is tried again at the next slot")
        return "retry later"
    write(ROOT / "failed" / f"{reel_id}.json", {**reel, "reason": reason})
    for suffix in (".mp4", ".jpg", ".json"):
        (ROOT / "reels" / f"{reel_id}{suffix}").unlink(missing_ok=True)
    print(f"{reel_id}: {reason}; given up after {TRIES} slots")
    return "failed"


def main() -> None:
    for details in sorted((ROOT / "reels").glob("*.json")):     # oldest first: ids start with the date
        reel_id = details.stem
        if not wait_until_online(PAGES + f"reels/{reel_id}.mp4"):
            print(f"{reel_id} is not online yet (GitHub Pages is still publishing it); the next run tries again")
            return
        reel = json.loads(details.read_text(encoding="utf-8"))
        try:
            print("Result:", post(reel_id, reel))
        except Refused as err:
            if re.search(r"code 190\b|HTTP 401", str(err)):
                # The token, not the reel: fail the run (GitHub emails the owner) and keep the reel.
                raise SystemExit(f"Instagram refused the token: {err}")
            print("Result:", give_up_later(reel_id, reel, str(err)))
        return                     # one reel per slot
    print("Nothing waiting to be posted.")


if __name__ == "__main__":
    main()
