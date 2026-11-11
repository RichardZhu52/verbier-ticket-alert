# Verbier Opening Session ticket alert

Sends an urgent push notification to your phone the moment **CHF 5 Opening Session tickets**
(Saturday 5 December 2026) come back in stock on the official Verbier 4Vallées shop:

- [Opening Session – ticket only (410650)](https://verbier4vallees.ch/en/online-shop/activities/opening-session_activity_410650)
- [Opening Session + St. Nicholas Express train from Geneva (426820)](https://verbier4vallees.ch/en/online-shop/activities/opening-session-st.nicholas-express_activity_426820)

It only **watches and tells you**. You still book by hand, logged into your own account.

## What we know about the drops

From the official pages (checked 11 Nov 2026):

- Tickets are sold **online only, during November**. "Every day, at different times, a certain
  number of tickets with or without a train journey go on sale." A limited number of tickets
  is released **Monday to Friday**.
- CHF 5 applies only if you **booked a place and turn up dressed as St Nicholas**. Without a
  costume you pay the normal Verbier 1-day price. Annual pass holders don't need a ticket but
  still need the costume. Proceeds support the Annapurna Association.
- About 2,000 places in total (verbier.ch). The shop's own availability data shows how many
  have been released so far: on 11 Nov at 13:30, **805** ticket-only and **196** with-train
  places had been released, and **0** were left in either.

Observed drop times (from another person's public 5-minute monitor, train product only):

| Day | First stock seen (Swiss time) |
|---|---|
| Tue 10 Nov 2026 | between 08:01 and 08:05 |
| Wed 11 Nov 2026 | between 08:15 and 08:20 |

That's why polling is fastest (every 20 s) from **07:30 to 10:00 on weekdays**. Every drop this
monitor sees is written to [`data/events.csv`](data/events.csv) (time, product, tickets left,
total released), so the pattern gets clearer each day.

## How it works

The shop page loads stock from a public JSON endpoint that its own booking form calls:

```
GET https://verbier4vallees.ch/en/shop/product/available-dates-times?activity_id=410650
{"success":true,"availabilityByDate":{"410650-2026-12-05":{"available":0,"max":805,...}}}
```

`monitor.py` (Python standard library only) polls that endpoint and sends alerts on these events:

| Event | Alert |
|---|---|
| `available` goes from 0 to more than 0 | **Urgent** (priority 5, breaks through Do Not Disturb if you allow it), with an "Open shop" button |
| more tickets released while still in stock | **Urgent** |
| sold out again | Quiet, says how long the drop lasted |
| 8 failed checks in a row, site blocks us, robots.txt changes | Warning |
| each run starts | Quiet heartbeat, so you know it's alive |

Polling rate:
- weekdays 07:30–10:00: every 20 s
- weekdays 06:00–23:30: every 60 s
- weekends: every 3 min
- stops automatically after 4 Dec 2026

It runs on GitHub Actions. Each job polls for about 5 h 30 min, then starts its own successor.
A cron every 30 minutes restarts the chain if it ever breaks; the extra runs see an active
run and exit within seconds.

## Setup (about 5 minutes)

### 1. Get the ntfy app and a free account

1. Install **ntfy** ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) /
   [iOS](https://apps.apple.com/app/ntfy/id1625396347)).
2. Create a free account at <https://ntfy.sh/signup>. Then go to **Account → Access tokens** and
   create a token.
   *Why:* anonymous ntfy.sh limits messages per IP address, and GitHub runners share IPs with
   other users, so alerts can be silently rate-limited. With a token, the limits count against
   your account.
3. Pick a long random topic name, e.g. `verbier-` followed by 16 random characters. Anyone who
   knows the name can read it, so keep it secret. In the app, tap **+** and subscribe to it.
   Log into your account in the app too.
4. Android: long-press the topic, then **Notification settings**, and allow it to override Do
   Not Disturb. iOS: urgent alerts appear as time-sensitive.

### 2. Optional backup channel: Telegram

1. Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. Send any message to your new bot. Then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.

### 3. Add GitHub secrets

In the repo, open **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `NTFY_TOPIC` | your secret topic name (required) |
| `NTFY_TOKEN` | your ntfy access token (strongly recommended) |
| `NTFY_EMAIL` | optional: ntfy also emails you on urgent alerts |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional backup channel |

### 4. Test, then start

1. Go to **Actions → Send test alert → Run workflow**. Your phone should buzz within seconds,
   showing current stock.
2. Go to **Actions → Ticket monitor → Run workflow**. After that it keeps itself running.

### Optional tuning (Settings → Variables)

| Variable | Default | Meaning |
|---|---|---|
| `PRODUCTS` | `410650,426820` | `410650` = ticket only, `426820` = with train. Set to one ID to watch only that product |
| `MIN_TICKETS` | `1` | e.g. `2` if you need two places in one go |
| `HOT_WINDOW` | `07:30-10:00` | fastest polling window (Swiss time, weekdays) |
| `HOT_POLL_SECONDS` / `POLL_SECONDS` | `20` / `60` | polling intervals |
| `ACTIVE_HOURS` | `06:00-23:30` | Swiss time |
| `HEARTBEAT` | `true` | set to `false` to drop the quiet "monitor running" message |

## Be ready when it buzzes

- Be **logged into** your verbier4vallees.ch account on your phone beforehand, with payment
  details ready. Drops seem to sell out within minutes.
- Have the shop page bookmarked. The alert's button opens it directly.

## Legal and fair use

This is designed to stay well within the law and the site's rules:

- **Public data only.** It reads the same unauthenticated stock number that the public page
  shows every visitor. No login, no personal data, no CAPTCHA solving, no proxies, no
  identity rotation.
- **robots.txt respected.** It is checked at the start of every run, and the monitor halts if
  the endpoint is ever disallowed. Today it is allowed.
- **Light load.** At most one request per product every 20 s at peak, which is fewer requests
  than one person loading the page once. It identifies itself honestly in its `User-Agent`
  with a link to this repo.
- **Backs off.** It obeys `429`/`503` and `Retry-After`, and **stops completely** on
  `401`/`403`. It never tries to get around a block.
- **No automated buying.** You purchase manually, so it doesn't compete unfairly in the
  checkout or hold stock. The site's terms (General Terms, §5) forbid reproducing site content
  for commercial or distribution purposes. This is personal, non-commercial, and redistributes
  no content. Lift passes are personal and non-transferable, so don't resell.
- If Verbier 4Vallées asks you to stop, stop: disable the workflow.

GitHub note: public repos get free Actions minutes. GitHub's Actions terms say Actions should
relate to the repo's software project, so this is a small personal, time-limited use (one
month). Disable the workflow after 4 December.

## Run locally

```bash
python3 -m unittest -v test_monitor
ONCE=true NTFY_TOPIC=your-topic python3 monitor.py   # single check
python3 monitor.py                                    # loop
```
