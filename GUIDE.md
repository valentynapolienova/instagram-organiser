# Using Instagram organiser

Start with the setup commands in the [README](README.md). Use
`.venv/bin/python` for every command below. This picks the Python environment
where you installed the package, even if your shell's `python` points elsewhere.

## 1. Export your saved posts

For a small first export:

```sh
.venv/bin/python ordered.py export --batch trial --amount 10
```

For your full library:

```sh
.venv/bin/python ordered.py export --batch library --amount 0
```

`--amount 0` means all saved posts that Instagram returns. Deleted or unavailable
posts may be missing. The script lists unreadable entries in `skipped.json`.
You can use `unordered.py` for exports too; both versions export the same data.

The first run asks for your username and password in Terminal. Password input
is hidden. If Instagram asks for 2FA, enter your code there too. You can also use
an unused Instagram backup code. The prompt alone does not mean Instagram sent
an SMS or WhatsApp message.

Later runs reuse the saved session. With one session file, you will not need
to enter your username again. With several, the username selects an account.
If a saved session fails, the script stops without trying a new password login.

The export goes in `private-data/<batch>/`. Each fetched page is saved before
the script reads its posts. If an export stops, you can resume with the same
batch name and amount. It will reuse the pages already saved. After a challenge
or rate limit, check Instagram before trying again.

A finished export cannot overwrite a review. Use a new batch name for a fresh
export, such as `library-2`.

## 2. Review the posts

Open `private-data/library/review.csv` in Excel, Numbers, or a CSV editor.
Every post starts as `keep`. Edit only the `action` and `collection` columns.
The [README](README.md) shows the three actions.

Choose categories after reading the captions and opening posts when needed.
Create each collection in Instagram with a unique name. The name in the CSV
must match it exactly. The script adds to existing collections; it does not
create or rename them. Each CSV row supports one target collection.

For recipe reviews, treat ingredient hints as questions to check. A caption
can say "vegan chicken", omit an ingredient, or use a language the hints miss.
Open the post if it is unclear. Leave it as `keep` until you decide.
Under a vegetarian rule that allows eggs and dairy, review meat, fish, seafood,
meat stock, and gelatin for removal. The script makes no food decisions itself.

Save your edits back to UTF-8 CSV at the same path. An Excel or Numbers workbook
alone will not update the CSV. Keep every item ID and column name unchanged.
If you sort the sheet, sort whole rows together.

## 3. Pick the version

Use `ordered.py` for newest-saved-first collections. It uses the save order from
the export. It also sorts existing posts in each collection targeted by an
`add` row. A `keep` post may change position there, while staying saved and in
the same collections. Other collections stay unchanged.

Exact sorting needs a saved rank for every post in a target collection.
A ten-post trial can only sort collections whose posts are in that export.
Use a full export for larger collections. If the script finds a post outside
the export, it stops before writing. Export a fresh full batch in that case.

Use `unordered.py` for fewer requests. It adds only missing posts, up to 30 in
each request. It checks membership after adding, but does not promise an order.
Instagram can shuffle posts inside a batch and show later batches first.

Both versions keep the same delays. Large ordered runs can take time because
each required move uses one request. Already-correct collections need no writes.
Choose your version before applying a batch. Once writes begin, changes to the
plan or version require a new reviewed batch.

## 4. Preview, then apply

An ordered preview:

```sh
.venv/bin/python ordered.py apply --batch library
```

This reads your local files. It does not log in or contact Instagram.
To apply the review:

```sh
.venv/bin/python ordered.py apply --batch library --execute
```

For unordered collections, replace `ordered.py` with `unordered.py` in both
commands. If you marked posts as `unsave`, add `--allow-unsave`:

```sh
.venv/bin/python ordered.py apply --batch library --execute --allow-unsave
```

Unsave removes a post from Saved entirely, including its collections. It is
not a way to remove a post from just one collection. The review keeps its link,
so you can save it again manually if the post is still available. This does
not restore its earlier collection memberships.

Apply checks the account and every target collection before its first write.
After each collection is updated, it reads all its pages to check the result.
The ordered version checks both membership and order. The unordered version
checks membership. An Instagram success message alone is not enough.

Use one running copy at a time. Don't edit collections in Instagram while an
apply or sort is running. The script checks for changes before continuing a
paused collection, but it cannot stop edits made during its requests.

## Sort existing collections

To keep your current collections and just change their order:

```sh
.venv/bin/python ordered.py sort --batch library
.venv/bin/python ordered.py sort --batch library --execute
```

The preview is local, so it cannot count the live moves. Execute reads all
current MEDIA collections and puts their newest saved posts first. It keeps
your manual additions and removals. It does not apply the CSV's actions,
unsave posts, or delete and recreate collections.

The original save ranks live in `inventory.json`; do not edit that file.
Older exports can recover their order from cached pages. If neither source
has the full order, make a new export. The CSV's row order cannot prove when
you saved a post.

## If a run stops

The script records writes in the batch folder:

| File | What it records |
| --- | --- |
| `results.json` | Confirmed unsave requests |
| `collections-results.json` | Collection additions and their checks |
| `sorting-results.json` | Sorting moves and their checks |

Repeating an unchanged batch skips completed work. The saved checks describe
that run; a repeat does not check every completed collection again. Use a new
export batch to sort again after later edits.

A `pending` entry means a request may have reached Instagram without a clear
result. A `failed` entry means the live result did not match the plan. The
script blocks further writes in either case. Check Instagram and compare the
result with the saved log before fixing it. Do not delete the log to force a
retry. Collection logs also track order and progress, so clearing one flag
without checking the rest can repeat a write.

For an unsave only, check whether the pending post is still saved. If it was
removed, add its pending string to `done` and remove `pending`. If it is still
saved, remove only `pending` before retrying. Get help with the log if unsure.

The script disables transport retries and uses one attempt per API operation
after login. It stops on a challenge and does not solve it automatically.

## Log out when finished

```sh
.venv/bin/python ordered.py logout
.venv/bin/python ordered.py logout --execute
```

The first command previews. The second loads the existing script session and
makes one logout request. It never logs in just to log out. When Instagram
confirms logout, the script deletes its local session file. Your exports and
review files stay on disk. A later export or apply will need a new login.

If logout fails, the session file stays in place and the script does not retry.
Use Instagram's account settings to check the session. Logging out is useful
when you are done; it is not a way to remove account limits.

## Keep private files out of GitHub

`private-data/` contains account sessions and saved-post data. Keep it private.
The repository's `.gitignore` excludes that folder, session files, spreadsheets,
and local logs. Never force-add these files. Share only code, tests, and docs.

The original `organise.py` command still works. It defaults to ordered mode;
`--fast-collections` selects unordered mode for `apply`. The two named scripts
are shortcuts to the same code, so fixes apply to both versions.
