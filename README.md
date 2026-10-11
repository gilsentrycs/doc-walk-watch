# DOC Great Walk availability watcher

Watches New Zealand DOC huts and campsites for cancellations and sends you a
**Telegram** message the moment your trip becomes bookable.

Great Walks sell out within minutes of the booking season opening, but people
cancel constantly. This checks every 30 minutes so you don't have to.

* **No server required.** Runs free on GitHub Actions.
* **No dependencies.** One Python file, standard library only.
* **No DOC account needed.** It only reads public availability. It never books
  or pays for anything — you do that yourself when it tells you to.

---

## Setting it up

### 1. Make a Telegram bot (2 minutes)

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts. It gives you a **token** that looks
   like `8123456789:AAH1a2B3c4D...`.
3. Send any message (`hi`) to your new bot so it is allowed to reply to you.
4. Find your chat id:

   ```bash
   export TELEGRAM_BOT_TOKEN='paste-your-token'
   python3 doc_watch.py --setup-telegram
   ```

   It prints something like `TELEGRAM_CHAT_ID=123456789`.

### 2. Choose your trips

`watch.json` already contains a working example. Edit it to describe your own
trips — or start over from scratch with `cp watch.example.json watch.json`.

See **Describing a trip** below. To check it before committing to anything:

```bash
python3 doc_watch.py --check     # prints a table, sends nothing
```

### 3. Put it on GitHub (this is what replaces a server)

1. Create a repository and push these files to it. Public is fine and gets
   unlimited free Actions minutes; see the note below.
2. In the repo go to **Settings → Secrets and variables → Actions → New
   repository secret** and add two secrets:
   * `TELEGRAM_BOT_TOKEN`
   * `TELEGRAM_CHAT_ID`
3. Go to the **Actions** tab and enable workflows if prompted.
4. Press **Run workflow** on "DOC availability watch" to test it immediately.

That's it. It now checks every 30 minutes, for free, whether or not your
computer is on.

> **Public or private?** Your Telegram token is safe either way — it lives in
> Actions secrets, never in the repo, and GitHub masks it in logs. The only
> difference is that on a public repo anyone can read the run logs, so your
> trip dates and the huts you're chasing are visible. That's usually fine.
> Choose private if you'd rather keep your plans to yourself.

---

## Describing a trip

A trip is an **ordered list of nights**. Night 1 is the start date, night 2 is
the next day, and so on. A trip only matches when **every** night has room for
`party_size` people on **consecutive** days, and all of them fall inside your
window.

```json
{
  "party_size": 2,
  "trips": [
    {
      "name": "Routeburn, 2 nights",
      "type": "greatwalk",
      "walk": "Routeburn Track",
      "window": { "start": "2027-02-10", "end": "2027-02-20" },
      "nights": [
        { "stay": ["Routeburn Flats Hut", "Routeburn Flats Campsite"] },
        { "stay": ["Lake Mackenzie Hut"] }
      ]
    }
  ]
}
```

That reads as: *somewhere between 10 and 20 February, two consecutive nights —
first at Routeburn Flats (hut **or** campsite, whichever is free), then at Lake
Mackenzie Hut — with room for 2 people.*

| Field | Meaning |
| --- | --- |
| `party_size` | How many people. Applies to all trips. |
| `name` | Any label. Used in the alert and to remember what it already told you. |
| `walk` | Great Walk name. Partial names work (`"Routeburn"`). |
| `window` | The earliest and latest dates you could start **or** finish. |
| `nights` | One entry per night, in order. |
| `stay` | Acceptable places for that night. Any one of them counts. |
| `enabled` | Set to `false` to pause a trip without deleting it. |

**One night is a valid trip.** Just give `nights` a single entry, and it alerts
on any night in the window.

Add as many trips as you like — different walks, different dates, backup plans.
Each is tracked and alerted separately.

### Finding the exact names

```bash
python3 doc_watch.py --list-walks                 # the 10 Great Walks + bookable period
python3 doc_watch.py --list-huts "Kepler Track"   # exact hut/campsite names to put in "stay"
```

Names must match exactly, because a typo silently watching nothing would be
worse than an error. If one doesn't match, the run fails loudly and tells you
the valid options.

### Huts that aren't Great Walks

Places like Mueller Hut use a different booking system, so they need ids
instead of a walk name:

```bash
python3 doc_watch.py --find-place "Mueller"
```

```json
{
  "name": "Mueller Hut",
  "type": "facility",
  "place_id": 747,
  "facility_id": 2487,
  "window": { "start": "2027-02-05", "end": "2027-02-09" },
  "nights": [ { "stay": ["Mueller Hut"] } ]
}
```

If `--find-place` can't list the facilities, open the hut's page on the DOC
site and read both numbers out of the URL:
`bookings.doc.govt.nz/Web/Default.aspx#!park/747/2487` → `place_id` 747,
`facility_id` 2487.

---

## Commands

```bash
python3 doc_watch.py --check            # availability table; sends nothing, saves nothing
python3 doc_watch.py --dry-run          # full run, but print alerts instead of sending
python3 doc_watch.py --test-telegram    # confirm Telegram works
python3 doc_watch.py --self-test        # offline logic tests; no network, no credentials
python3 doc_watch.py                    # real run
```

`--check` output, where `-` is full and a number is places free:

```
  Routeburn, 2 nights
                              02-10 02-11 02-12 02-13 02-14
    Lake Mackenzie Hut            -     -     -     -     -
    Routeburn Flats Campsite     21    19    29    21    28
    Routeburn Flats Hut           -     -     -     -     1
    -> nothing for 2
```

---

## When it messages you

Only when something **changes**:

* a trip becomes bookable that wasn't,
* a new start date appears,
* capacity changes materially (2 → 10 places alerts; 17 → 15 doesn't),
* availability disappears and later comes back.

It will **not** nag you every 30 minutes about the same vacancy. What it
already told you is remembered in `state.json`, which the workflow commits back
to your repo.

It also sends one warning if nothing has worked for 6 hours, because a broken
watcher looks exactly like "no availability" and that's the dangerous failure.

Transient DOC outages are retried with backoff and never message you.

---

## Running it on a laptop instead

GitHub Actions is recommended because it runs whether or not your machine is
on. But if you'd rather run it locally:

```bash
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_CHAT_ID='...'
*/30 * * * * cd /path/to/doc-walk-watch && /usr/bin/python3 doc_watch.py >> watch.log 2>&1
```

(`crontab -e` on Linux; on macOS use the same, but note the job only runs while
the machine is awake.)

---

## Things worth knowing

* **Only the booking season is visible.** `--list-walks` prints the bookable
  period. Dates outside it will always show as unavailable — that means "not
  released yet", not "full".
* **A vacancy is not a reservation.** Someone else can take it while you're
  reading the message. Book fast.
* **GitHub disables scheduled workflows after 60 days of repo inactivity.** It
  emails you first. Pressing **Run workflow** occasionally, or any commit,
  resets the clock.
* **Actions minutes are unlimited on public repos** and 2,000/month free on
  private ones. This uses a few seconds per run either way.
* **DOC's API is undocumented** and could change. If it does, runs fail
  visibly rather than quietly reporting no availability.
* **Until you create `watch.json`** the scheduled run does nothing and reports
  "Not configured yet" as a warning, rather than failing and emailing you every
  30 minutes. A `watch.json` that exists but is broken *does* fail loudly.
* Be a good citizen: every 30 minutes is plenty. Don't lower it much.

---

## If you are an AI assistant setting this up

Your job is to turn a sentence like *"I want to do the Routeburn over two
nights sometime in mid-February 2027, two of us"* into a working `watch.json`.

1. **Discover real names first — never guess them.**
   ```bash
   python3 doc_watch.py --list-walks
   python3 doc_watch.py --list-huts "Routeburn"
   ```
   Hut names must match the API exactly. A guessed name fails the run.

2. **Work out the night chain.** An N-night walk is N entries in `nights`, in
   the order they'll be walked. Check the route direction on DOC's website if
   the order matters — huts are typically walked in a fixed sequence, and the
   watcher enforces consecutive days.

3. **Offer hut *and* campsite** in the same night's `stay` list when the user
   doesn't mind which, since it roughly doubles the chance of a hit.

4. **Make the window wider than the trip.** A 2-night trip needs at least 3
   days of window to have any chance; a wider window finds more options.

5. **Verify before declaring success:**
   ```bash
   python3 doc_watch.py --check        # confirms names resolve and shows real data
   python3 doc_watch.py --self-test    # confirms the matching logic
   ```
   `--check` reporting `nothing for 2` is a normal, healthy result for a
   popular walk. An **error** about unknown facilities means you got a name
   wrong. Do not confuse the two.

6. **Never commit the Telegram token.** It belongs in GitHub Actions secrets or
   environment variables, never in `watch.json` or any file in the repo.

7. **Never book anything.** This tool is read-only by design. Report
   availability to the user and let them book it themselves.
