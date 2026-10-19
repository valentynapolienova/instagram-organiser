"""Verified collection updates using the behaviour observed on this account."""
import hashlib
import json
import random
import time


def single_attempt_requests(client):
    # private_request retries timeouts even when transport retries are disabled.
    def request(endpoint, data=None, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        if client.authorization:
            headers.setdefault("Authorization", client.authorization)
        if client.delay_range:
            time.sleep(random.uniform(*client.delay_range))
        return client._send_private_request(endpoint, data=data, headers=headers, **kwargs)
    client.private_request = request


def paginated(client, endpoint, params):
    seen = set()
    while True:
        result = client.private_request(endpoint, params=dict(params))
        if not isinstance(result, dict) or result.get("status") != "ok" or not isinstance(result.get("items"), list):
            raise ValueError("Invalid collection response; stopped without retrying.")
        yield from result["items"]
        following = result.get("next_max_id") or result.get("max_id")
        if not following:
            if result.get("more_available"):
                raise ValueError("Missing collection pagination cursor.")
            return
        if following in seen or not result["items"]:
            raise ValueError("Collection pagination stalled.")
        seen.add(following)
        params = {**params, "max_id": following}


def list_collections(client):
    # Client.collections() catches errors and can silently return a partial list.
    return list(paginated(client, "collections/list/", {
        "collection_types": '["ALL_MEDIA_AUTO_COLLECTION","PRODUCT_AUTO_COLLECTION","MEDIA"]',
        "max_id": "",
    }))


def media_id(item):
    media = item.get("media", item) if isinstance(item, dict) else None
    pk = str(media.get("pk") or media.get("id") or "").split("_")[0] if isinstance(media, dict) else ""
    if not pk.isdigit() or int(pk) <= 0:
        raise ValueError("Unreadable media ID.")
    return pk


def ordered_members(client, cid):
    ids = [media_id(item) for item in paginated(client, f"feed/collection/{cid}/", {"include_igtv_preview": "false"})]
    if len(ids) != len(set(ids)):
        raise ValueError("Repeated collection item; stopped.")
    return ids


def saved_order(batch, inventory):
    order = inventory.get("order")
    if order is None:
        # Legacy exports kept their original order in cached pages, not inventory.
        order, seen = [], set()
        allowed = set(inventory["items"])
        for path in sorted((batch / "pages").glob("*.json")):
            for item in json.loads(path.read_text())["items"]:
                try:
                    pk = media_id(item)
                except ValueError:
                    continue
                if pk in allowed and pk not in seen:
                    order.append(pk)
                    seen.add(pk)
    if not isinstance(order, list) or len(order) != len(set(order)) or set(order) != set(inventory["items"]):
        raise ValueError("Original saved order is unavailable. Export a new full batch; CSV row order cannot establish save dates.")
    return order


def moves_to_front(current, desired):
    """Minimum move-to-front writes, including additions; never remove members."""
    if len(current) != len(set(current)) or len(desired) != len(set(desired)) or not set(current) <= set(desired):
        raise ValueError("Invalid collection membership plan.")
    positions = {pk: i for i, pk in enumerate(current)}
    boundary, previous = len(desired), len(current)
    for i in range(len(desired) - 1, -1, -1):
        position = positions.get(desired[i])
        if position is None or position >= previous:
            break
        boundary, previous = i, position
    return list(reversed(desired[:boundary]))


def apply_collections(client, batch, inventory, actions, save_json, *, fast=False, sort_all=False):
    additions = {}
    for action in actions:
        if action["action"] == "add":
            additions.setdefault(action["collection"], []).append(action["id"])
    if not additions and not sort_all:
        return
    order = [] if fast else saved_order(batch, inventory)
    rank = {pk: i for i, pk in enumerate(order)}
    signature = hashlib.sha256(json.dumps([inventory["account"], actions, order, fast, sort_all], sort_keys=True).encode()).hexdigest()
    path = batch / ("sorting-results.json" if sort_all else "collections-results.json")
    journal = json.loads(path.read_text()) if path.exists() else {"signature": signature, "records": []}
    if "pending" in journal or journal.get("failed"):
        raise ValueError("Uncertain collection result. Inspect the collection journal before continuing; see GUIDE.md.")
    if journal["signature"] != signature:
        raise ValueError("This collection plan changed after execution began. Use a new reviewed batch.")
    if not path.exists():
        collections = list_collections(client)
        if sort_all:
            targets = [c for c in collections if c.get("collection_type") == "MEDIA"]
        else:
            targets = []
            for name in additions:
                matches = [c for c in collections if c.get("collection_name") == name and c.get("collection_type") == "MEDIA"]
                if len(matches) != 1:
                    raise ValueError(f"Collection {name!r} missing or ambiguous. Create a uniquely named collection in Instagram.")
                targets.append(matches[0])
        # Read and validate every target before the first mutation.
        for target in targets:
            cid, name = str(target.get("collection_id", "")), target["collection_name"]
            if not cid.isdigit():
                raise ValueError("Invalid collection identity.")
            current = ordered_members(client, cid)
            desired = list(dict.fromkeys(current + additions.get(name, [])))
            if not fast:
                if any(pk not in rank for pk in desired):
                    raise ValueError(f"Collection {name!r} contains posts outside this export. Export a new full batch before sorting.")
                desired.sort(key=rank.__getitem__)
            updates = [pk for pk in desired if pk not in set(current)] if fast else moves_to_front(current, desired)
            size = 30 if fast else 1
            journal["records"].append({"id": cid, "name": name, "expected": current, "desired": desired,
                                       "chunks": [updates[i:i + size] for i in range(0, len(updates), size)], "completed": 0})
        save_json(path, journal)
    writes = sum(len(r["chunks"]) - r["completed"] for r in journal["records"] if not r.get("verified"))
    print(f"Collection plan: {writes} remaining write requests; {'unordered batches of at most 30' if fast else 'newest-saved-first, minimum moves'}.", flush=True)
    for record in journal["records"]:
        if record.get("verified"):
            continue
        cid, name = record["id"], record["name"]
        actual = ordered_members(client, cid)
        unchanged = set(actual) == set(record["expected"]) if fast else actual == record["expected"]
        if not unchanged:
            raise ValueError(f"Collection {name!r} changed since planning. Stopped to preserve manual edits.")
        for index in range(record["completed"], len(record["chunks"])):
            chunk = record["chunks"][index]
            journal["pending"] = {"collection": cid, "chunk": index, "ids": chunk}
            save_json(path, journal)
            response = client.private_request(f"collections/{cid}/edit/", client.with_action_data({"added_media_ids": json.dumps(chunk)}))
            if not isinstance(response, dict) or response.get("status") != "ok":
                raise ValueError("Instagram did not confirm collection update.")
            if response.get("collection_id") and str(response["collection_id"]) != cid:
                raise ValueError("Collection response identity mismatch.")
            for pk in chunk:
                if pk in record["expected"]:
                    record["expected"].remove(pk)
                record["expected"].insert(0, pk)
            record["completed"] = index + 1
            del journal["pending"]
            save_json(path, journal)
            if (index + 1) % 10 == 0:
                print(f"Updated {name}: {index + 1}/{len(record['chunks'])} requests", flush=True)
        actual = ordered_members(client, cid)
        matches = set(actual) == set(record["desired"]) if fast else actual == record["desired"]
        if not matches:
            journal["failed"] = {"collection": cid, "actual": actual, "expected": record["desired"]}
            save_json(path, journal)
            raise ValueError(f"Collection verification failed for {name!r}; stopped without retrying.")
        record["verified"] = True
        save_json(path, journal)
        print(f"Verified {name}: {len(actual)} posts ({'membership only' if fast else 'newest saved first'}).", flush=True)
