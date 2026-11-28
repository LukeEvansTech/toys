# toys

My statue and garage-kit collection, and a daily watch for new listings I might like.

- **Collection** and **On order**: what I own, what I paid, who from, and the orders still to
  chase. The records are built outside the app and loaded as one file; they live in the app's
  database, never in this repository.
- **New**: listings the shops added, judged against my likes by a model. A daily email carries
  the picks with photos and a coverage line per shop; a day with nothing to show is silent, and
  a Monday with nothing says the watch still runs.
- **A page per listing**: the shop's photos (up to 12, at up to 1600 pixels), with the scale,
  size, deposit and release date its description states.
- **Likes**: the characters I collect per franchise and the ones I never want, edited in the app.

Nothing sexualised is shown or mailed. Shops label some of it, so a keyword list hides those
before any model call; the model judges the rest from the photo and the title, and a listing it
did not answer for is held back until it does.

Built on [collectorkit](https://github.com/LukeEvansTech/collectorkit).

## Run it

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python -m toys load collection collection.json
python -m toys load profile profile.json
python -m toys sweep --sample 4    # judge the 4 newest per shop: no mail, nothing marked seen
python -m toys backlog --cap 400   # judge what the baseline stored; mail only the picks
python -m toys serve
```

Settings are `TOYS_*` environment variables (`toys/config.py` and collectorkit's `Common`).
Without `TOYS_LLM_URL` nothing is judged, so nothing is shown. The shops without a product feed
need the `obscura` binary on `PATH`.

After `uv pip compile requirements.in -o requirements.txt`, put the collectorkit tag back in
`requirements.txt`: the compiler writes the commit, and a commit is never offered an update.
