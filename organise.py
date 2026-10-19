"""Review saved Instagram media, then explicitly apply collection/unsave actions."""
import argparse
import csv
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import sys
from collection_ops import apply_collections, saved_order, single_attempt_requests

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "private-data"
FIELDS = ["item", "url", "author", "caption", "hint", "action", "collection"]
ANIMAL = re.compile(
    r"\b(chicken|beef|pork|bacon|ham|lamb|turkey|duck|veal|venison|"
    r"fish|salmon|tuna|anchov(?:y|ies)|sardines?|shrimp|prawns?|seafood|"
    r"crab|lobster|mussels?|oysters?|squid|octopus|gelatin|gelatine|"
    r"lard|sausage|chorizo|pepperoni|bone broth|fish sauce|oyster sauce)\b", re.I
)


def hint(caption):
    # shortcut: English caption clues only; add transcription/vision for video analysis.
    matches = sorted({m.group(0).lower() for m in ANIMAL.finditer(caption)})
    if matches:
        return "Review animal-ingredient words: " + ", ".join(matches)
    return "Review ingredients; no English animal-ingredient keywords found"


def cell(text):
    """Prevent untrusted captions/usernames becoming spreadsheet formulas."""
    text = str(text).replace("\x00", "")
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def login():
    from instagrapi import Client
    from instagrapi.exceptions import TwoFactorRequired

    sessions = sorted(STATE.glob("*-session.json"))
    if len(sessions) == 1:
        username = sessions[0].name.removesuffix("-session.json")
    else:
        username = input("Instagram username (selects a saved session if present): ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.]+", username):
        raise ValueError("Invalid username")
    client = Client()
    client.delay_range = [3, 6]
    client.with_challenge_flow = False
    session = STATE / (username.lower() + "-session.json")
    if session.exists():
        client.load_settings(session)
        client.set_retry_config(session_retry_total=0)
        if not client.user_id:
            raise ValueError("Saved session has no account identity. Stopped without attempting a new login.")
        print(f"Reusing the saved session for {username}; no password login or 2FA request.")
        # The requested operation will validate the session. Never call login()
        # here: that method can automatically start a fresh password login.
        single_attempt_requests(client)
        return client
    client.set_retry_config(session_retry_total=0)
    password = getpass.getpass("Instagram password (hidden; not stored by this script): ")
    try:
        client.login(username, password)
    except TwoFactorRequired:
        print("Instagram requires 2FA. This prompt does not confirm an SMS or WhatsApp code was sent.")
        print("You can enter an unused 8-digit Instagram backup code from your account's 2FA settings.")
        code = getpass.getpass("Instagram verification or backup code (hidden): ").strip()
        if not code:
            raise ValueError("No verification code entered. No further login attempt made.")
        client.login(username, password, verification_code=code)
    client.dump_settings(session)
    single_attempt_requests(client)
    return client


def logout_saved_session():
    sessions = sorted(STATE.glob("*-session.json"))
    if not sessions:
        print("No saved script session to log out.")
        return
    if len(sessions) != 1:
        raise ValueError("More than one saved session exists. Select the account before logging out.")
    from instagrapi import Client

    client = Client()
    client.delay_range = [3, 6]
    client.with_challenge_flow = False
    client.load_settings(sessions[0])
    client.set_retry_config(session_retry_total=0)
    if not client.user_id:
        raise ValueError("Saved session has no account identity. No login or logout attempted.")
    single_attempt_requests(client)
    if not client.logout():
        raise RuntimeError("Instagram did not confirm logout. The session file was kept; no retry was made.")
    sessions[0].unlink()
    print("Instagram confirmed logout. Removed the local script session file. Your exports and reviews are still here.")


def saved_row(client, item):
    """Read only the fields we need; Instagram sometimes omits Media.code."""
    if not isinstance(item, dict):
        raise ValueError("Saved item is not an object")
    media = item.get("media", item)
    if not isinstance(media, dict):
        raise ValueError("Saved media is unavailable")
    pk = str(media.get("pk") or media.get("id") or "").split("_")[0]
    if not re.fullmatch(r"[0-9]+", pk) or int(pk) <= 0:
        raise ValueError("Saved item has no usable media ID")
    code = media.get("code") or media.get("shortcode")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        code = client.media_code_from_pk(pk)  # Local numeric conversion; no API request.
    caption = media.get("caption") or media.get("caption_text") or ""
    if isinstance(caption, dict):
        caption = caption.get("text") or ""
    caption = caption if isinstance(caption, str) else ""
    user = media.get("user") or {}
    author = (user.get("username") or "") if isinstance(user, dict) else ""
    return {
        "item": "ig:" + pk,
        "url": f"https://www.instagram.com/p/{code}/",
        "author": cell(author),
        "caption": cell(caption),
        "hint": hint(caption),
        "action": "keep",
        "collection": "",
    }


def export(client, batch, amount):
    batch.mkdir(exist_ok=True)
    if (batch / "review.csv").exists():
        raise ValueError("This batch already has a review file; it will not be overwritten.")
    meta_path = batch / "export.json"
    metadata = {"account": str(client.user_id), "amount": amount}
    if meta_path.exists():
        if json.loads(meta_path.read_text(encoding="utf-8")) != metadata:
            raise ValueError("Resume requires the same account and --amount as the original export.")
    else:
        if any(batch.iterdir()):
            raise ValueError("Existing batch has no export checkpoint; choose a new batch name.")
        save_json(meta_path, metadata)
    pages = batch / "pages"
    pages.mkdir(exist_ok=True)
    rows, ids, cursors, skipped = [], set(), set(), []
    cursor = ""
    page_number = 0
    while True:
        page_number += 1
        page_path = pages / f"{page_number:06d}.json"
        cached = page_path.exists()
        if cached:
            page = json.loads(page_path.read_text(encoding="utf-8"))
            if page["request_cursor"] != cursor:
                raise ValueError("Cached pagination is inconsistent; stopped without refetching.")
        else:
            params = {"include_igtv_preview": "false"}
            if cursor:
                params["max_id"] = cursor
            result = client.private_request("feed/saved/posts/", params=params)
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise ValueError("Unexpected saved-feed response; stopped without retrying.")
            following = result.get("next_max_id") or result.get("max_id") or ""
            if result.get("more_available") and not following:
                raise ValueError("Instagram reports more saves but supplied no pagination cursor.")
            page = {"request_cursor": cursor, "items": result["items"], "next_cursor": str(following)}
            # Save before parsing, so malformed media never requires another fetch.
            save_json(page_path, page)
        for position, item in enumerate(page["items"], 1):
            try:
                row = saved_row(client, item)
            except ValueError as error:
                skipped.append({"page": page_number, "position": position, "reason": str(error)})
                continue
            pk = row["item"][3:]
            if pk in ids:
                continue
            ids.add(pk)
            rows.append(row)
            if amount and len(rows) >= amount:
                break
        following = page["next_cursor"]
        print(f"Page {page_number} ({'cached' if cached else 'fetched'}): {len(rows)} items; {len(skipped)} unavailable rows.", flush=True)
        if (amount and len(rows) >= amount) or not following:
            break
        if not page["items"] or following in cursors:
            raise RuntimeError("Pagination stalled. Cached pages are preserved; stopped without retrying.")
        cursors.add(following)
        cursor = following
    save_json(batch / "inventory.json", {"account": str(client.user_id), "items": sorted(ids),
                                        "order": [row["item"][3:] for row in rows]})
    save_json(batch / "skipped.json", skipped)
    temporary = batch / "review.csv.tmp"
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(batch / "review.csv")
    print(f"Exported {len(rows)} items to {batch / 'review.csv'}. No saved items changed.")
    if skipped:
        print(f"{len(skipped)} unavailable rows could not be exported; see {batch / 'skipped.json'}.")


def read_plan(batch):
    inventory = json.loads((batch / "inventory.json").read_text(encoding="utf-8"))
    allowed = set(inventory["items"])
    actions, seen = [], set()
    with (batch / "review.csv").open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not set(FIELDS).issubset(reader.fieldnames or []):
            raise ValueError("Missing CSV columns; preserve the original column names.")
        for line, row in enumerate(reader, 2):
            item = (row.get("item") or "").strip()
            if not re.fullmatch(r"ig:\d+", item) or item[3:] not in allowed or item in seen:
                raise ValueError(f"CSV row {line}: unknown, altered or duplicate item ID")
            seen.add(item)
            action = (row.get("action") or "").strip().lower()
            collection = (row.get("collection") or "").strip()
            if action not in {"keep", "add", "unsave"}:
                raise ValueError(f"CSV row {line}: action must be keep, add or unsave")
            if (action == "add") != bool(collection):
                raise ValueError(f"CSV row {line}: only add requires a collection name")
            if action != "keep":
                actions.append({"id": item[3:], "action": action, "collection": collection})
    return inventory, actions


def apply(client, batch, inventory, actions, allow_unsave, *, fast=False, sort_all=False):
    if str(client.user_id) != inventory["account"]:
        raise ValueError("This batch belongs to a different Instagram account.")
    if any(a["action"] == "unsave" for a in actions) and not allow_unsave:
        raise ValueError("Unsave means remove from Saved entirely. Add --allow-unsave to enable it.")
    journal_path = batch / "results.json"
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
    if "pending" in journal:
        raise ValueError("An earlier operation has an uncertain result. Check it in Instagram before continuing; see GUIDE.md.")
    if (any(key.startswith("add:") for key in journal.get("done", []))
            and any(a["action"] == "add" for a in actions)
            and not (batch / "collections-results.json").exists()):
        raise ValueError("This batch used the older collection writer. Use sort to reorder current members, or a new reviewed batch for additions.")
    apply_collections(client, batch, inventory, actions, save_json, fast=fast, sort_all=sort_all)
    done = set(journal.get("done", []))
    for entry in actions:
        if entry["action"] != "unsave":
            continue
        key = f"unsave:{entry['id']}:"
        if key in done:
            print("Already completed:", key)
            continue
        journal["pending"] = key
        save_json(journal_path, journal)
        ok = client.media_unsave(entry["id"])
        if not ok:
            raise RuntimeError("Instagram did not confirm success; stopped.")
        done.add(key)
        journal = {"done": sorted(done)}
        save_json(journal_path, journal)
        print("Instagram reported success:", key)
    print("Finished. Check the resulting collections and Saved in Instagram.")


def main(variant=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = ["export", "apply", "logout"]
    if variant != "unordered":
        commands.append("sort")
    parser.add_argument("command", choices=commands)
    parser.add_argument("--batch", default="trial", help="Name of a local review batch")
    parser.add_argument("--amount", type=int, default=10, help="Export count; 0 fetches all available saved items")
    parser.add_argument("--execute", action="store_true", help="Apply changes or log out; otherwise preview only")
    parser.add_argument("--allow-unsave", action="store_true", help="Permit removal from Saved entirely")
    if variant is None:
        parser.add_argument("--fast-collections", action="store_true", help="Add in batches of up to 30; does NOT guarantee order")
    args = parser.parse_args()
    args.fast_collections = (variant == "unordered" and args.command == "apply") or getattr(args, "fast_collections", False)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.batch) or args.amount < 0:
        parser.error("Use a simple batch name and a non-negative amount.")
    if args.fast_collections and args.command != "apply":
        parser.error("--fast-collections applies only to apply; sorting requires individual updates.")
    STATE.mkdir(exist_ok=True, mode=0o700)
    batch = STATE / args.batch
    if args.command == "logout":
        if args.execute:
            logout_saved_session()
        else:
            print("Preview only. Add --execute to log out of the saved script session and remove its local session file.")
    elif args.command == "export":
        if (batch / "review.csv").exists():
            raise ValueError("This batch already has a review file; it will not be overwritten.")
        export(login(), batch, args.amount)
    elif args.command == "sort":
        inventory = json.loads((batch / "inventory.json").read_text())
        saved_order(batch, inventory)
        print("Sort every current collection by original saved order, newest first; preserve current membership.")
        if args.execute:
            apply(login(), batch, inventory, [], False, sort_all=True)
        else:
            print("Preview only. No login or Instagram requests. Add --execute to inspect live collections and sort them.")
    else:
        inventory, actions = read_plan(batch)
        for entry in actions:
            print(entry["action"].upper(), "media ID " + entry["id"], entry["collection"])
        print(f"{len(actions)} reviewed actions. Existing collection memberships are preserved.")
        if any(a["action"] == "add" for a in actions):
            if args.fast_collections:
                print("Fast mode: batches of at most 30; item order is NOT guaranteed.")
            else:
                saved_order(batch, inventory)
                print("Target collections will be sorted newest saved first, including their existing members.")
        if not args.execute:
            print("Preview only. No login or Instagram writes. Add --execute to apply.")
        elif actions:
            if any(a["action"] == "unsave" for a in actions) and not args.allow_unsave:
                raise ValueError("Add --allow-unsave only if you intend to remove marked items from Saved entirely.")
            apply(login(), batch, inventory, actions, args.allow_unsave, fast=args.fast_collections)


def run_cli(variant=None):
    try:
        main(variant)
    except KeyboardInterrupt:
        print("\nStopped. If interrupted during apply, check private-data results before retrying.", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        # Do not dump raw Instagram responses, which may contain session data.
        if isinstance(error, ModuleNotFoundError):
            print(f"Stopped: missing Python module {error.name!r}.", file=sys.stderr)
            print(f"Using Python: {sys.executable}", file=sys.stderr)
            command = shlex.join([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
            print(f"Install dependencies with:\n{command}", file=sys.stderr)
        elif isinstance(error, (ValueError, RuntimeError, FileExistsError, FileNotFoundError)):
            print(f"Stopped: {error}", file=sys.stderr)
        else:
            print(f"Stopped: {type(error).__name__}. No automatic re-login or challenge resolution was attempted. Check Instagram before retrying.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run_cli()
