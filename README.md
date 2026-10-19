# Instagram organiser

Put your saved Instagram posts and reels into collections. Export your saves,
review a CSV, then apply the changes you chose. You can also unsave selected posts.

This uses [instagrapi](https://github.com/subzeroid/instagrapi), an unofficial
Instagram API. Instagram may limit requests or ask you to verify your account.
The script stops on errors. It cannot promise that your account will avoid limits.

## Choose a version

| Version | What it does | Best for |
| --- | --- | --- |
| `ordered.py` | Puts the most recently saved posts first. Skips correct collections and makes only the moves needed. | When collection order matters |
| `unordered.py` | Adds missing posts in batches of up to 30. Checks that the posts arrived, but does not sort them. | Fewer requests when order does not matter |

Both use the same code and local data folder. Choose one before you apply a review.
Switching versions partway through a batch is blocked.

The ordered version follows when you saved each post, not when someone published
it. The unordered version can leave posts in a different order. A later batch
can appear above an earlier one.

## Set up

Use Python 3.10 or newer. The tests have been run with Python 3.13.
These commands are for macOS or Linux:

```sh
git clone https://github.com/valentynapolienova/instagram-organiser.git
cd instagram-organiser
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Start with an export

```sh
.venv/bin/python ordered.py export --batch library --amount 0
```

Enter your Instagram login in Terminal. The script saves a session locally for
later runs. Exporting reads your saved posts; it does not change them.

Open `private-data/library/review.csv`. Keep the column names and item IDs.
Create the collections in Instagram, then fill in `action` and `collection`:

| action | collection | Result |
| --- | --- | --- |
| `keep` | leave empty | Keep the post saved and in its current collections |
| `add` | an exact collection name | Add the post to that collection |
| `unsave` | leave empty | Remove the post from Saved entirely |

Preview the ordered version, then apply when the review is ready:

```sh
.venv/bin/python ordered.py apply --batch library
.venv/bin/python ordered.py apply --batch library --execute
```

For the faster version, use `unordered.py` in both commands instead.
If your review includes unsaves, the apply command also needs `--allow-unsave`.

Read the [guide](GUIDE.md) for login help, sorting existing collections, and
recovering from an interrupted run.

## What stays local

Session files, captions, reviews, and run logs live in `private-data/`.
That folder is excluded from Git. Never upload it or share a session file.

The script does not choose categories or judge recipes for you. Its English
ingredient hints help with review, but they can miss ingredients or flag vegan
substitutes. It does not watch videos, read images, or call an AI service.

## Tests

```sh
.venv/bin/python -m unittest -v test_organise.py
```

Tests use fake clients and do not contact Instagram. The code keeps requests
sequential, with a 3 to 6 second delay. It saves progress before each write and
checks collection contents after updates.
