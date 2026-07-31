# Running the capture 24/7 on macOS

## Moving from another machine

Almost everything is in git. Three things are not, and only one of them matters.

| item | how | why |
| --- | --- | --- |
| code | `git clone` | all of it |
| `.env` | retype, ~10 lines | gitignored; **paths change** (`C:\Users\...` becomes `/Users/...`) |
| private key | copy the file, or generate a fresh Live key | gitignored, and never committed |
| `captures/` | **copy this** | gitignored and *irreplaceable* — Kalshi publishes no historical order books, so a lost capture is gone permanently |
| `data/*.csv`, `*.jsonl` | optional | all re-fetchable in about half an hour |

Then verify the machine before trusting it:

```bash
python -m scripts.check_setup
```

It checks the Python version, dependencies, `.env`, that the private key parses, that a *signed*
request to Kalshi actually succeeds (the only check that proves the key matches the environment —
a Demo key returns 401 against Live), that the backfill sources are reachable, and that the capture
directory is writable with room to grow. Exit code is non-zero on failure, so it also works as a
gate in a script.


For a dataset, uptime *is* the product: Kalshi publishes no historical order books, so any hour the
capture is down is an hour that cannot be recovered later. A laptop that sleeps is the wrong host; a
machine that stays on with a supervised service is the right one.

`launchd` is the right supervisor on macOS — it starts the capture at boot, restarts it if it exits,
and needs no login session.

## Capture on the Mac, develop elsewhere

A sensible split when the development machine is not always on: the Mac mini runs the capture
around the clock, and analysis happens wherever you like. **The Mac does not need Claude Code for
this** — only Python, the repo, credentials, and the service.

Point the capture at a synced folder so completed days appear on the other machine automatically:

```bash
python capture_live.py --series KXBTC15M --hours 2 --refresh-minutes 5 \
    --export-dir ~/Dropbox/kalshi-captures
```

Only *finished*, compressed days are exported. Today's file is still being appended to, and a sync
client replicating a half-written file hands the other machine a truncated final line — so it stays
behind until the day rolls over. Exports are idempotent, so nothing is copied twice.

On the analysis machine, point the tools at the synced folder:

```bash
python -m scripts.build_timeseries --dir ~/Dropbox/kalshi-captures -o data/timeseries.csv
```

The trade-off is latency: you get data one day behind. To analyse the current day, copy `captures/`
off the Mac directly (`scp`) instead of waiting for the export.

## Setup

```bash
# 1. Clone and install
git clone <your-repo> ~/kalshi-crypto-arb
cd ~/kalshi-crypto-arb
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Credentials — a LIVE key, read-only if Kalshi offers one
cp .env.example .env
# then set KALSHI_LIVE_API_KEY_ID and KALSHI_LIVE_PRIVATE_KEY_PATH

# 3. Verify it runs in the foreground FIRST. Never install a service you have not seen work.
.venv/bin/python capture_live.py --series KXBTC15M --hours 2 --refresh-minutes 5
#    Expect: "Subscribed to N market(s)", then "capture alive: ..." every 5 minutes.
#    Ctrl+C once you have seen a heartbeat.

# 4. Install the service
mkdir -p ~/Library/LaunchAgents ~/kalshi-crypto-arb/logs
sed "s|__HOME__|$HOME|g" deploy/com.kalshi.capture.plist > ~/Library/LaunchAgents/com.kalshi.capture.plist
launchctl load ~/Library/LaunchAgents/com.kalshi.capture.plist
```

## Operating it

```bash
launchctl list | grep kalshi          # is it registered?
tail -f ~/kalshi-crypto-arb/logs/capture.log
launchctl unload ~/Library/LaunchAgents/com.kalshi.capture.plist   # stop
```

**Stop the Mac sleeping**, or the service is pointless — a sleeping machine records nothing:

```bash
sudo pmset -a sleep 0 disksleep 0
sudo pmset -a womp 1        # wake on network, useful after a power blip
```

## Checking it is actually recording

A capture that has silently stopped producing data looks identical to a healthy one from the outside,
which is why the process logs a heartbeat with event counts. Confirm independently:

```bash
python -m backtest capture --dir captures     # event counts and time span
```

Watch two things over time: the span should extend to *now*, and `ws` events should keep climbing.
A frozen count with a running process means the subscription died without the process noticing.

## Disk

Roughly 150 bytes an event, so a busy series is on the order of 150 MB/day uncompressed. Completed
day files are gzipped automatically (about 5-10x), and readers handle `.gz` transparently. A year is
tens of gigabytes — trivial for a Mac mini, but worth a periodic glance:

```bash
du -sh ~/kalshi-crypto-arb/captures
```
