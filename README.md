# Market Research Monitor

A standalone Python 3.11+ monitor for synthetic intraday option fixtures. It starts OFF. It has no brokerage, live-price, trading, cloud, or notification adapters. Every displayed candidate is **RESEARCH CANDIDATE — NOT A BUY SIGNAL**. Reports always show **NO TRADE** and **zero permitted contracts**.

Run the offline suite from this directory. It uses the Python standard library and denies sockets, DNS, and subprocesses.

```sh
python3 -I -B monitor_top10/tests/offline.py
```

For a local fixture replay, use an absent output directory. This writes synthetic JSON and a historical dashboard. The replay makes no external requests and cannot stand in for a GitHub run.

```sh
python3 -B -m monitor_top10.actions.runner --rehearse --output _monitor
```

Running without a mode leaves the monitor OFF. The [workflow template](monitor_top10/actions/synthetic-monitor.workflow.disabled.yml) is inactive and kept outside GitHub’s registered workflow directory.

See the [phone and reporting guide](PHONE_GUIDE.md) for the future owner-approved GitHub procedure.
