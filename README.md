# AI ESS Controller

A Home Assistant integration that plans battery charge, discharge, import and
export against a half-hourly tariff, using a solar forecast it corrects from your
own history and a load forecast that learns how your house responds to the
weather.

Built for a DIY setup — Solax X1 Hybrid G4, a Renault Zoe pack behind a
[Battery Emulator](https://github.com/dalathegreat/Battery-Emulator), ~2 kW of
PV, Octopus Agile — but every value is configurable, and the inverter and battery
are behind abstractions so it is not tied to that hardware.

**It ships disarmed.** On a new install the controller plans, publishes and logs
exactly what it *would* do, and touches nothing. You arm it with the
`Inverter control` switch when you are satisfied the plan makes sense.

---

## What it actually does

Every five minutes:

1. Reads live state — inverter mode, battery SoC, PV and load power.
2. Folds the completed half-hour into its learning model.
3. Fetches forward prices, the solar forecast and the weather.
4. Optimises the next 36 hours (configurable) in half-hour slots.
5. Works out what to do *now*, respecting any override or strategy lock.
6. Applies it to the inverter — verifying the write actually landed.

### The optimiser

A cost-minimising **dynamic program** over the battery's state of charge,
discretised into levels (60 by default). For a 48-hour horizon this runs in
about 10 ms in pure Python.

Dynamic programming rather than linear programming, deliberately:

- **No dependencies.** Nothing to compile, nothing to break on a Raspberry Pi.
  `requirements` in the manifest is empty.
- **It handles the awkward parts of a real tariff exactly** — negative Agile
  prices, export caps, curtailment, asymmetric round-trip losses, a battery wear
  allowance.
- **It is deterministic.** The same inputs always give the same plan, which
  matters when the thing is spending your money.

The cost of a slot depends only on *how much the battery moved*, never on the
absolute SoC it started from — so each slot's costs are priced once and reused
across every level. That is what keeps it fast enough to re-plan every cycle.

Each slot is priced as: grid import × import price, minus exported energy ×
export price, plus a wear allowance on throughput. Energy left in the battery at
the end of the horizon is valued at the horizon's mean import price, so the
optimiser does not dump the battery into the final slot just because the horizon
stops there.

### Learning

Not an LLM, and not a black box. Observations are binned by the conditions that
actually drive them, and each bin holds an exponentially weighted mean:

- **Solar** — keyed by season, half-hour and sky bucket. "This weather, this time
  of year, this hour → you generated this much."
- **Load** — keyed by season, day type, half-hour and outdoor temperature bucket.
  The temperature bucket is what makes **A/C visible to the planner**: after a few
  hot afternoons, the 25–30 °C bucket for 14:00 carries a much higher mean than
  the 15–20 °C bucket, so a forecast hot day automatically produces a bigger
  overnight charge. Season captures what temperature cannot — chiefly day length,
  so a 12 °C December evening (lights and cooking on) is not confused with a
  12 °C June one.

If you have a solar forecast integration (Solcast, Forecast.Solar), its *shape*
is trusted and a **correction factor** is learned per season/hour/sky bucket.
That factor absorbs everything a generic forecast cannot know about your specific
installation — shading from a tree or chimney, panel soiling, a mis-declared
azimuth, inverter clipping. Learning a ratio converges far faster than learning
absolute output from scratch. With no forecast source at all, it learns absolute
generation from your own history instead.

Buckets degrade gracefully — a specific bucket with too few samples falls back to
a broader one, then to a sensible default — so the plans are reasonable on day
one and sharpen over a few weeks. The `Learning progress` sensor tells you where
you are; expect roughly two weeks to settle.

---

## Installation

### HACS (recommended)

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/Eastiswest/Home-ESS-HA-controller-`, category
   **Integration**.
3. Install **AI ESS Controller**, restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → AI ESS Controller**.

### Manual

Copy `custom_components/ess_controller` into your `config/custom_components/`
directory and restart.

---

## What you need first

| Thing | Why | Required? |
|---|---|---|
| An inverter integration exposing mode + limit entities | This is how control is applied | To control; not to advise |
| A tariff source | Nothing to optimise without prices | Yes |
| A house load power sensor | The load model cannot learn without it | Strongly recommended |
| A PV power sensor | The solar model cannot learn without it | Strongly recommended |
| A weather entity | Sky condition for solar, temperature for A/C load | Recommended |
| A solar forecast integration | Better day-ahead solar than history alone | Optional |

For a Solax inverter, install
[wills106/homeassistant-solax-modbus](https://github.com/wills106/homeassistant-solax-modbus)
first. This integration drives that one's entities; it does not speak Modbus
itself.

---

## Configuration

Setup is a short wizard, and every section can be revisited later from the
integration's **Configure** menu.

### Battery

Nameplate capacity, the SoC window, power limits, efficiencies, wear allowance.

Two things worth getting right for a second-life EV pack:

- **Measured capacity sensor.** A Zoe 22 kWh pack that has aged to 17 kWh should
  be planned against 17. If your BMS reports real capacity, point this at it and
  it overrides nameplate. Alternatively supply a state-of-health sensor and tick
  *Scale nameplate capacity by reported health*.
- **State of charge sensor.** Point this at the Battery Emulator's own SoC rather
  than the inverter's estimate — the BMS reading comes from the real pack. If you
  leave it empty, the inverter's figure is used, which is what an OEM battery
  without a separate BMS integration needs.

**Emergency reserve** is distinct from **minimum SoC**. The minimum is the floor
the optimiser plans down to; the reserve is written to the inverter as *its* own
minimum and sits below that. It is the hardware backstop if this integration
stops running, so the optimiser is deliberately not allowed to plan into it.

### Inverter

Pick your inverter type and, if you have more than one, an entity name prefix
(e.g. `solax`) to disambiguate them. Entities are auto-discovered by role — "the
thing that selects the working mode", "the thing that limits charge current" —
and any of them can be overridden individually under **Configure → Inverter
entities** if discovery gets one wrong.

**Nominal battery voltage** matters: Solax limits battery charge and discharge by
*current* in amps, not power, so a power target has to be divided by pack
voltage. The live voltage sensor is used when available (an EV pack's terminal
voltage swings widely between empty and full, so a fixed assumption would miss
the intended power by tens of percent) and this figure is the fallback.

### Tariff

Import prices are required. Export is optional — leave it as **None** if you are
not paid for export, and the optimiser will treat exported energy as worthless,
storing or spilling surplus rather than giving it away.

Four ways to get prices, for either direction:

- **Octopus Energy integration** — read rates from
  [BottlecapDave's integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy)
  if you already run it.
- **Octopus API (direct)** — talks to the public Octopus API itself, no other
  integration needed. Give it a product code (`AGILE-24-10-01`) plus your region
  letter, or a full tariff code, or **just your account number and an API key**
  and it will look the tariff codes up for you. The code is validated during
  setup, so a typo fails there rather than silently producing no prices.
- **Another integration's rate entity** — the parsing is deliberately tolerant
  and handles Nord Pool, Tibber, Amber and hand-built template sensors.
- **Fixed rate** or a **time-of-use schedule** like
  `00:30-04:30=9.5,04:30-00:30=28.0` (local wall-clock time, wraps midnight).

Prices are normalised internally to pence per kWh. Sources publishing pounds are
detected automatically; override it under *Price units* if the guess is wrong.

When you add an export tariff later, switch the export provider and the optimiser
starts arbitraging into it — no rewrite needed.

### Forecasting

Point it at your solar forecast sensors (add both today and tomorrow to cover the
horizon), a weather entity, and your PV / load / grid power sensors.

**Any HA weather integration works** — the code reads whichever fields your
provider publishes rather than requiring one specific schema. It picks the best
available sky signal, in this order:

1. **Numeric cloud cover** (`cloud_coverage`) — Met.no, OpenWeatherMap, AccuWeather.
2. **UV index** — used when a provider gives UV but no cloud cover. **This is the
   UK Met Office case**: its hourly forecast carries condition, temperature and
   `uv_index`, but no cloud coverage. UV is a good stand-in because the bucket key
   already pins season and half-hour, so solar elevation is held roughly constant
   and the *variation* in UV is mostly cloud attenuation.
3. **Condition string** — `sunny` / `partlycloudy` / `cloudy` mapped to an
   approximate cloud cover. Only six levels, so this is the weakest option.

Which one is in use is reported in the diagnostics download under
`weather.sky_signal`. Buckets encode the signal type (`c3` vs `u6`), so if you
change weather provider the old buckets simply stop being used and new ones build
alongside rather than silently mixing two different scales.

Met Office does provide hourly **temperature**, which is what the A/C load model
needs, so the load side is unaffected. If you want the strongest solar signal,
adding Met.no (which does publish `cloud_coverage`) purely as a second weather
entity is a reasonable move — or add Solcast, in which case its forecast shape is
used and the sky signal only drives the learned correction factor.

**Typical daily consumption** is only used for slots the load model has not
learned yet, spread over a typical domestic diurnal shape rather than assumed
flat — a flat assumption badly misprices the evening peak.

### Optimiser

Horizon length, SoC resolution, end-of-horizon valuation, and the permission
switches.

Agile publishes tomorrow's prices around 16:00, so for much of the day the
horizon runs past known data. The remainder is extrapolated from the same time of
day, which preserves the overnight trough and evening peak that dominate the
plan. Extrapolated slots are flagged, and the `Import price now` sensor's
`known_until` and `extrapolated_slots` attributes tell you exactly where
certainty ends.

---

## Going live

1. Install and configure. Leave `Inverter control` **off**.
2. Watch `sensor.<name>_planned_action` and the plan in
   `sensor.<name>_planned_horizon_cost`'s `slots` attribute for a few days.
   Compare `Planned saving vs self-use` against what your system does now.
3. Check `Control status` reads `advisory (dry run)` and that the log lines make
   sense — they show the exact writes that would be made.
4. When satisfied, turn `Inverter control` on.
5. Watch `binary_sensor.<name>_inverter_write_problem`. If it triggers, writes
   are being accepted and dropped — see below.

### Tuning

The single most useful dial is **Battery wear allowance** (p/kWh cycled). Raise
it and the optimiser only takes big price spreads; lower it and it cycles more
aggressively. Set it to roughly *pack cost ÷ expected lifetime throughput*. For a
cheap salvage pack this is genuinely low, which is exactly why aggressive
arbitrage can pay for a DIY build where it would not for a new one. The
`Usable battery capacity` sensor's `spread_needed_to_cycle` attribute shows the
minimum price spread that currently justifies a cycle.

---

## Entities

**Sensors** — `planned_action`, `next_action`, `control_status`,
`planned_horizon_cost`, `planned_saving_vs_self_use`, `import_price_now`,
`export_price_now`, `target_state_of_charge`, solar and load forecast for the
rest of today and tomorrow, `planned_grid_import` / `planned_grid_export`,
`battery_state_of_charge`, `usable_battery_capacity`, `learning_progress`,
`planned_charge_power`, `planned_discharge_power`.

The full slot-by-slot plan lives in the `slots` attribute of
`planned_horizon_cost` — it is what makes the plan auditable, and it feeds an
ApexCharts card directly.

> **Exclude the plan sensor from the recorder.** That attribute is a 72-slot
> table rewritten every five minutes, which is a few megabytes a day of database
> growth for data that has no historical value. The live attribute still works
> normally for dashboards and templates:
>
> ```yaml
> recorder:
>   exclude:
>     entities:
>       - sensor.ai_ess_controller_planned_horizon_cost
>       - sensor.ai_ess_controller_import_price_now
> ```

**Binary sensors** — `charging_planned`, `discharging_planned`,
`exporting_planned`, `cheap_import_slot` (handy for scheduling a dishwasher or an
EV charge), `control_active`, `inverter_available`, `plan_problem`,
`inverter_write_problem`.

**Controls** — `Optimiser enabled`, `Inverter control`, `Allow grid charging`,
`Allow export`, `Allow battery export`; numbers for the SoC window, power limits,
wear allowance, typical daily load and the heating/cooling sensitivities; a
`Strategy` select; and buttons to re-plan, clear an override or reset learning.

**Services** — `ess_controller.replan`, `set_override`, `clear_override`,
`reset_learning`.

Overrides beat the strategy lock, which beats the plan. An override always
expires on its own, so a forgotten one cannot strand the battery:

```yaml
action: ess_controller.set_override
data:
  action: charge
  duration: { minutes: 90 }
  power: 3.0
```

---

## Known constraints, honestly

**Pocket WiFi 3.0 control is possible but flaky.** The dongle does support local
Modbus TCP read *and* write with firmware ≥ V3.004.03, so no extra hardware is
strictly needed. In practice it is widely reported as dropout-prone, and its
worst failure mode is silent: it accepts a Modbus write and discards it. Every
write here is therefore **read back and verified**, and a mismatch raises
`binary_sensor.<name>_inverter_write_problem` instead of being mistaken for
success. Identical commands are not rewritten each cycle, to avoid hammering the
link. If it proves unreliable, an RS485→TCP bridge (EW11, Waveshare) is a drop-in
swap — reconfigure the underlying Solax Modbus integration and nothing here
changes. Gen4 dropped the built-in Ethernet, so the dongle really is the only
no-extra-hardware path.

**Solax rejects setting writes while locked.** Unlocking is handled
automatically as part of applying a command, because a locked inverter is the
most common reason control appears to do nothing at all.

**Load learning needs a load sensor.** Without one, load forecasts stay on the
default profile forever and the plans will be noticeably worse.

**Price extrapolation is a weak forecast.** Beyond published data it is
persistence — the same time of day, yesterday. It preserves the shape but will be
wrong on magnitude. Shorten the horizon if that bothers you.

**Not tested against live hardware.** The logic has 298 automated tests,
including adapter tests against a simulated Solax entity set covering the
dropped-write failure mode. That is not the same as having run on a real
inverter. Start in advisory mode.

---

## Development

```bash
pip install pytest ruff
python -m pytest tests/ -q
ruff check custom_components tests
ruff format custom_components tests
```

The decision-making code — optimiser, learning, forecast parsing, tariff
parsing, inverter adapters — is written **without Home Assistant imports** and
interacts with HA only through `hass.states` and `hass.services`. That means it
all runs under plain pytest on any Python, with no HA test harness, and the
adapters can be driven against a stub. A test enforces this boundary, because
letting an HA import creep in would silently cost the suite its coverage of the
parts that matter most.

```
custom_components/ess_controller/
├── optimiser/dp.py        # the dynamic program
├── learning/              # EWMA buckets + persistence
├── forecast/              # solar, load, weather, resampling
├── tariff/                # Octopus API, entity readers, fixed, time-of-use
├── inverter/              # roles, discovery, Solax + generic adapters, battery
├── coordinator.py         # the planning loop
├── settings.py            # live-tunable settings (HA-free)
└── config_flow.py         # setup and options
```

## Licence

MIT
