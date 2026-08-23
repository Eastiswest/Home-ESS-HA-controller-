# AI ESS Controller

Home Assistant integration for tariff-aware battery dispatch. Plans charge,
discharge, import and export in half-hour slots against Octopus Agile (or a
fixed/TOU tariff), using solar and load forecasts it corrects from your own
measured history.

Built around a SolaX X1 Hybrid G4 via
[homeassistant-solax-modbus](https://github.com/wills106/homeassistant-solax-modbus),
but hardware-agnostic behind an adapter layer.

**Ships disarmed.** On a new install it plans and logs what it *would* do, and
writes nothing until you arm the `Inverter control` switch.

## Features

- Dynamic-programming optimiser: half-hourly plan over a 36–48 h horizon,
  deterministic, no external solver, no Python dependencies
- Handles negative prices, export caps, curtailment, round-trip losses and a
  battery wear allowance (derivable from pack cost)
- Learns solar and load from your sensors; bins by weather, time and season
- Hourly solar forecasts from Solcast, Forecast.Solar or any energy-platform
  integration; keeps headroom for the sun to beat its forecast
- Octopus Saving Sessions and Power Ups
- Flexible load shifting with optional appliance switching
- Storm/outage anticipation; steps back entirely during a power cut (EPS)
- Tariff comparison against your own learned usage
- Performance log with self-use and no-battery counterfactuals; CSV export
- Auto-installed dashboard; faults raised in the Repairs panel
- Every inverter write is verified, deadbanded and rate-limited to protect the
  EEPROM

## Requirements

| What | Needed for |
|---|---|
| Inverter integration with mode/limit entities | Control (not needed to advise) |
| Tariff source (Octopus product code or fixed rates) | Required |
| House load power sensor | Load learning |
| PV power sensor | Solar learning |
| Weather entity | Solar/heating/cooling forecasts |
| Solar forecast integration | Better day-ahead solar |
| Octopus Energy integration | Grid sessions |

## Install

1. HACS → custom repositories → add this repo as **Integration**
2. Download **AI ESS Controller**
3. Restart Home Assistant
4. Settings → Devices & services → **Add integration** → AI ESS Controller
5. Complete the wizard (Battery and Import tariff are the two steps that
   matter; the rest have defaults)

Step 4 is not optional — without a config entry nothing runs.

**Updating:** update from HACS, then restart. Configuration, learned history
and the performance log survive. Never uninstall to update.

## Going live

Run in advisory mode for a few days. When the plan looks right, arm
`Inverter control`. Overrides, a strategy lock and a `Re-plan now` button are
on the dashboard; diagnostics can be downloaded from the device page.

## Known constraints

- Pocket WiFi dongle writes can be flaky; every write is read back and a
  mismatch raises `inverter_write_problem`
- Load learning needs a load sensor; without one forecasts stay on defaults
- Price extrapolation beyond published data is persistence, not prediction
- Tariff comparison scores a 24–48 h window, not a year
- Not tested against live hardware beyond one real install — start disarmed

## Development

```bash
pip install pytest ruff jinja2
ruff check custom_components tests && ruff format --check custom_components tests
python -m pytest tests/ -q
```

The full suite in `tests/test_with_homeassistant.py` needs Home Assistant
installed and runs in CI.

Releases: bump the version in `manifest.json` **and** `const.py`, commit, tag
`vX.Y.Z`, push the tag. The workflow refuses a tag that disagrees with the
manifest, and writes the release notes from the commit subjects.

## Licence

MIT
