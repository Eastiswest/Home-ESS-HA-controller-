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
is trusted and a **correction factor** is learned per season/hour/sky bucket. That
factor absorbs everything a generic forecast cannot know about your specific
installation — shading from a tree or chimney, panel soiling, a mis-declared
azimuth, inverter clipping. Learning a ratio converges far faster than learning
absolute output from scratch. With no forecast source at all, it learns absolute
generation from your own history instead.

The factor starts at exactly **1.0** and stays there until a bucket has enough
observations to be trusted. A forecast that already accounts for your tilt and
azimuth, with nothing shading the array, is therefore used at face value from the
very first plan — never second-guessed on the strength of one cloudy half-hour. A
ratio is a quotient of two small numbers, so a single slot where a cloud crossed a
forecast-clear sky would otherwise scale that bucket down by three or four times.

Buckets degrade gracefully — a specific bucket with too few samples falls back to
a broader one, then to a sensible default — so the plans are reasonable on day
one and sharpen over a few weeks. The `Learning progress` sensor tells you where
you are; expect roughly two weeks to settle.

### Negative prices

Agile goes negative often enough to matter, and being paid to import is only
worth as much as the headroom you have to absorb it. The optimiser handles this
without any special-case code, because emptying the battery beforehand is simply
the cheapest path through the horizon:

- It **discharges ahead of the window** to make room — into the house, into
  export if that pays, and if neither, it will export for nothing, because the
  headroom is worth more than the kWh given away.
- It **imports at full power throughout**, capped by your charge power and grid
  import limits.
- It **ends the window full**, ready for the expensive evening.

At a deeply negative price, dumping a kWh purely to re-import it is genuine
arbitrage even with **no export payment at all**: you give the kWh away, then get
paid to take it back. It pays whenever

```
|negative import price|  >  wear allowance × charge efficiency
```

which at a 2p/kWh allowance and 95% efficiency is any price below about −1.9p —
a low bar that Agile clears fairly often. At 8p/kWh it needs about −7.6p and so
happens rarely. The **wear allowance** therefore governs this entirely.

Note also that what leaves the property is capped by your **export limit**, and
only *generation* can be curtailed — battery discharge has to go somewhere. So on
a sunny day the array is turned down before the battery is asked to stop.

One caveat worth knowing: **`Allow battery export` off roughly halves what you can
capture**, because the battery can then only empty into the house, so it cannot
make much headroom before the window arrives.

### Setting the wear allowance

The wear allowance is the most influential number in the whole system: it sets how
big a price spread must be before cycling is worth it, and how negative import has
to go before the battery is emptied to make room. You can set it two ways.

**Enter it directly** in p/kWh, if you already know your figure.

**Or turn on `Derive wear from battery cost`** and give it what the pack cost,
its expected cycle life, and optionally a residual value. It then computes

```
wear per kWh = (pack cost − residual) ÷ (expected cycles × usable kWh)
```

For a £500 22 kWh pack over a 15–95% window (17.6 kWh usable):

| Expected cycles | Wear allowance | Lifetime throughput | Dump-to-reimport pays below |
|---|---|---|---|
| 800 | 3.55 p/kWh | 14,080 kWh | −3.37p |
| 1,000 | 2.84 p/kWh | 17,600 kWh | −2.70p |
| 1,500 | 1.89 p/kWh | 26,400 kWh | −1.80p |
| 2,000 | 1.42 p/kWh | 35,200 kWh | −1.35p |
| 3,000 | 0.95 p/kWh | 52,800 kWh | −0.90p |

The **cycle life figure must be for the SoC window you have configured**, not the
headline number a datasheet quotes at a shallower depth of discharge. Narrowing
the window raises the per-kWh cost (fewer kWh per cycle) but usually buys more
cycles, so the two partly cancel — which is why the window is worth experimenting
with once the rest is settled.

The `Wear allowance in use` sensor always shows which figure is actually in force,
derived or manual, with the workings and both practical thresholds — the spread
needed to cycle, and how negative import must go before dumping to re-import pays
— in its attributes.

Two things the linear model does not capture, and reasons not to set the allowance
too keenly: **depth-of-discharge effects** (95%→15% repeatedly is harder on a pack
than the same kWh shuffled round the middle) and **calendar ageing** (a pack that
dies of old age before its throughput is spent cost more per kWh than this says).

### Grid incentive sessions

Two Octopus schemes are worth real money, and both are handled by repricing the
tariff before optimisation — so the existing planner does the right thing without
any special-case logic.

- **Power Up / free electricity sessions** → import price is overridden to zero
  for the window, so the battery fills up.
- **Saving Sessions (National Grid Demand Flexibility Service)** → the reward
  rate is *added* to the import price for the window.

That second one is exact, not an approximation. DFS pays
`(baseline − actual) × rate`, and your baseline is fixed and exogenous, so
maximising the payment is identical to minimising `actual × rate` — which is
precisely an import price uplift. The optimiser then pre-charges before the
window, discharges through it, and refuses to import, because that is now the
cheapest path. Export counts towards reduction too, so the same uplift is applied
to the export price.

By default it acts **only on sessions you have joined** — discharging hard for a
session you never opted into earns nothing.

### Flexible load shifting

Moving a dishwasher or immersion heater into a cheap slot often beats battery
arbitrage outright, because the energy is bought cheaply *and* never pays
round-trip losses. Define loads as
`Dishwasher=1.2kWh@2kW,22:00-06:00; Immersion=3kWh@3kW` and they are scheduled
into the cheapest feasible windows.

The pricing is subtler than "lowest import price". Each window is costed against
the **marginal** cost of extra consumption given the current battery plan: in a
slot the plan already exports, extra load costs you the forgone *export* revenue;
in a slot the plan is spilling surplus PV, extra load is free. Loads are then
folded into the load forecast and the battery re-plans around them, so it
pre-charges for the immersion heater rather than being surprised by it. Largest
load is placed first, and the connection limit stops two of them stacking.

**No smart appliances required.** For most people the dishwasher has a dial and
the immersion has a mechanical timer, so by default this feature is pure advice:
the schedule is published and you act on it. `sensor.*_scheduled_flexible_loads`
carries an `advice` attribute that says exactly that — `start Immersion at 03:00`,
or `run Immersion now, until 04:00` — along with `next_load` and `next_start` for
templating, and the full per-load schedule in `placements`. A dashboard card and a
phone notification off that attribute is the whole workflow.

If an appliance *is* switchable from Home Assistant — a smart plug, a relay, an
`input_boolean` feeding your own automation, a `script` — add its entity as a
third field and the schedule can be driven for you:

```
Dishwasher=1.2kWh@2kW,22:00-06:00
Immersion=3kWh@3kW,00:00-07:00,switch.immersion
```

Switching is armed separately from inverter control, with the `Switch appliances`
switch, and needs advisory mode off as well. Two rules keep it well-behaved: it
only ever switches off something it switched on, so a machine you started
yourself is left alone; and once a load has been energised its finish time is
committed, so a re-plan that finds a marginally cheaper window later cannot
switch a running dishwasher off again. Loads with no entity stay advisory even
with the switch armed, and `binary_sensor.*_flexible_load_scheduled_now` works
either way.

### Power cut anticipation

Holds extra charge back when an outage looks likely, by raising the planning
floor — the mechanism the optimiser already respects. It can **only** raise the
floor, never lower one you set deliberately.

Three signals: forecast wind gusts (already in the weather data being fetched,
and gusts rather than mean wind because gusts bring lines down), any binary
sensor you nominate (a weather-warning integration, a DNO scraper, a manual
toggle), and a planned-outage calendar for DNO interruption notices.

Wind thresholds are in whatever unit your weather entity reports, which varies.
Check the `max_wind` attribute on `sensor.*_outage_risk` for a few days and set
the thresholds from what you actually see.

### Tariff comparison

A comparison site asks what an *average* house would pay. That is the wrong
question for a house with a battery: a tariff with a deep overnight trough and an
expensive peak is *better* for you and worse for someone without storage.

So the `Compare tariffs` button runs the actual optimiser against each candidate
tariff, using your learned load and solar profile, and ranks by projected cost
including standing charges. Same profile for every candidate, so the comparison
isolates the tariff. The `ess_controller.recommend_tariffs` service returns the
ranked table as response data.

Limits, stated on the result itself: Agile publishes only 24–48 hours ahead, so
the window is short and a few cheap days are not an annual saving. The wear
allowance is applied, so a tariff that only wins by cycling the pack twice as
hard does not look artificially good.

### Performance history you can export

Every completed half-hour is recorded — prices in force, measured solar, load and
grid flow, the forecasts that had been made for that slot, SoC either side, the
action planned and the action applied. 60 days by default, adjustable in the
optimiser step.

From that, the numbers that actually answer "is this working?":

| | |
|---|---|
| **Money** | What you spent, against two counterfactuals: no battery at all, and a battery running plain self-use. The second is the one that matters — beating "no battery" only proves a battery works. |
| **Wear** | The same saving after charging the extra cycling to the wear allowance, plus equivalent full cycles used. |
| **Forecasting** | Solar and load error per slot, MAE *and* signed bias. A model 2 kWh out in both directions is healthy; one quietly 2 kWh low every day is broken, and only the bias term tells them apart. |
| **Control** | How often the inverter actually did what the plan said, and the round-trip efficiency the pack really returned. |
| **Solar** | Self-consumption: the share of generation used on site rather than exported. |

`sensor.*_saving_vs_self_use_this_week` carries the whole report as attributes.
To get it out as a file:

```yaml
action: ess_controller.export_performance
data:
  days: 30
  format: csv
  write_file: true
```

The response contains the summary and the CSV text; `write_file` also drops
`config/ess_controller/performance_<entry>.csv`, which is the file to hand to a
spreadsheet or paste into an AI assistant and ask what to change. The last two
days of slots and both 7- and 30-day summaries are included in the normal
diagnostics download too, so a bug report carries evidence with it.

Caveats travel with the numbers rather than living here: a summary computed from
two days of data says so, an advisory-mode history says the difference is not the
optimiser's doing, slots with no grid meter are declared as unmetered, and a
window that ends with the battery much fuller than it started says how much that
flatters the cost.

Needs a grid power sensor to be metered rather than inferred — configure one in
the forecasting step. Without it the log still records solar, load, forecasts and
actions, and says which slots were unmetered.

---

## Installation

### HACS (recommended)

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/Eastiswest/Home-ESS-HA-controller-`, category
   **Integration**.
3. Download **AI ESS Controller**.
4. **Restart Home Assistant.** A reload is not enough — new files need a restart.
5. **Settings → Devices & services → + Add integration → AI ESS Controller**,
   and complete the setup flow.

**Step 5 is not optional.** Downloading through HACS only copies files onto
disk; until a config entry exists none of the integration's code runs, so there
are no entities and nothing in the sidebar. If you are unsure whether it is
configured, look for entities beginning `ess` in **Developer tools → States** —
a working install has around sixty.

Of the eleven setup steps, two matter on the first pass: **Battery** (your state
of charge sensor, the pack capacity, and what the pack cost if you want the wear
allowance derived) and **Import tariff** (Octopus product code and region).
Everything else has a sensible default and can be changed later without
reconfiguring. It arrives disarmed.

### Manual

Copy `custom_components/ess_controller` into your `config/custom_components/`
directory and restart.

### Updating

HACS shows an **Update** button for a repository that publishes releases, and
only a redownload for one that does not — so every version worth taking is
tagged and released. Update in place from HACS; your configuration, learned
history and performance log all survive. **Restart Home Assistant afterwards**:
HACS replaces the files, but Python keeps the old module in memory until a
restart, so a reload is not enough.

Uninstalling and reinstalling is never required, and would throw away your
configuration.

Cutting a release (for anyone maintaining a fork):

```bash
# bump the version in BOTH manifest.json and const.py, then
git commit -am "Release 0.6.1"
git tag v0.6.1 && git push && git push --tags
```

The release workflow refuses to publish if the tag, the manifest and the
constant disagree — a mismatch produces an update that installs and then reports
the old version, which is thoroughly confusing from the outside.

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
| The Octopus Energy integration | Saving Session and Power Up windows | Optional |
| A calendar / warning entity | Planned and storm-related outage anticipation | Optional |

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

**How the inverter is reached is not this integration's business.** It calls
entities that your existing inverter integration exposes, so Modbus TCP over a
WiFi dongle, RS485, or anything else all work identically from here. What matters
is that *something* in Home Assistant publishes **writable** entities for charge
and discharge control — a read-only integration can be read but not driven, and
the controller will stay stuck in advisory mode.

For a SolaX X1 Hybrid G4 that means
[homeassistant-solax-modbus](https://github.com/wills106/homeassistant-solax-modbus),
which speaks Modbus TCP to a Pocket WiFi dongle and creates the writable selects
and numbers this integration drives. The core `solax` integration is HTTP and
read-only, so it cannot be used for control — pick `None` for the adapter and run
advisory-only if that is all you have.

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

Product codes, since the naming trips people up: **import** Agile is
`AGILE-24-10-01`, **export** Agile is `AGILE-OUTGOING-19-05-13` — outgoing reads
`AGILE-OUTGOING`, not `OUTGOING-AGILE`. Open
<https://api.octopus.energy/v1/products/> in a browser to see the live list.
If you have no export tariff, set the export price source to **none** and the
export step is skipped entirely.

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

#### Predicting the prices Octopus has not announced

Octopus publishes tomorrow's Agile rates at about 16:00, so for most of the day
the planning horizon runs past the end of real prices. Something has to fill the
gap, and there are two options.

By default it is **day-ahead persistence**: yesterday's price at the same time of
day. That is honest and weak. It keeps the overnight trough and the evening peak
roughly where they belong, and it knows nothing whatsoever about tomorrow's wind.

Tick **Predict unannounced prices (AgilePredict)** on the import tariff step and
the tail comes from [AgilePredict](https://agilepredict.com) instead — an
ensemble model over weather and demand data, published per region, by the author
of PV Opt. There is a matching switch on the export step for Agile Outgoing.

Four things worth knowing:

- **It never overwrites a real price.** Predicted slots are trimmed to begin
  where the announced run ends. A guess replacing a known rate is the one outcome
  worse than having no forecast at all.
- **Predicted slots are marked.** They carry `price_is_forecast`, the plan table
  shows the price with an asterisk, and the learner ignores them so one model's
  error cannot compound into the next.
- **It degrades to persistence.** If the service is down, slow, or returns
  nonsense, the plan is built exactly as it was before and a warning goes in the
  log. Nothing about the system depends on a free third-party service staying up.
- **It scores itself.** AgilePredict returns a fortnight, so it always overlaps
  the announced window. That overlap is free marking: `price_forecast.accuracy`
  on the import price sensor carries the mean absolute error and the signed bias
  over the slots where both a prediction and a real price exist. Look at it after
  a week to decide whether it earns its place in your region.

Off by default, because it is a call to somebody else's server and nobody should
acquire one of those without saying yes to it. It needs your region letter, which
the Octopus API provider already collects; on the Octopus-integration provider
the region field appears next to the switch.

### Forecasting

**It works on day one, and improves from there.** That is a deliberate property,
because the alternative is driving the battery by hand for a week. Three layers,
best first, and each supersedes the one below it:

| Layer | Solar | Load |
|---|---|---|
| **Learned history** | Your own generation, bucketed by month, time of day and sky | Your own consumption, bucketed by time, weekday and temperature |
| **External forecast** | A solar forecast integration's hourly figures, corrected by a learned ratio that starts at 1.0 | — |
| **Physical / typical** | Clear-sky estimate from your latitude, array size and cloud cover | A typical domestic diurnal profile scaled to your daily total |

So on the very first plan, before anything has been learned: solar comes from the
sun's actual position and your array size, load comes from a typical profile
scaled to your daily kWh, and prices come from Agile. That is enough to charge
overnight at the right times and hold through the evening peak. Learning then
sharpens it — and if you map a solar forecast, the learned correction starts at
1.0, so the very first plan already uses it properly rather than waiting.


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

## The dashboard

You do not have to build one. On first setup a ready-made **ESS Controller**
dashboard is added to the sidebar, generated from the entities your install
actually has:

| View | What is on it |
|---|---|
| **Overview** | What it is doing now and why, state of charge, the arming switch, the next three hours as a table |
| **Plan** | The price shape as a sparkline, then **the whole horizon** slot by slot with a bar per price, grouped by day; prices, this half-hour's detail, 24 hours of history |
| **Performance** | The weekly report — spend against both counterfactuals, forecast error, plan fidelity, round-trip efficiency — plus the wear allowance workings |
| **Loads & events** | Flexible load schedule and advice, grid sessions, outage risk |
| **Settings** | Every live-tunable number and switch in one place |

Four deliberate properties:

- **Stock cards only.** ApexCharts and mini-graph-card make prettier plots, but
  they are separate HACS installs, and a dashboard that renders as a column of
  red "Custom element doesn't exist" boxes is worse than no dashboard. There is
  also no stock card that can plot the *future* at all — `history-graph` draws
  recorded state — so the plan is charted with block characters instead: a
  sparkline of the whole horizon, and a bar per slot in the detail table. Scaled
  to the cheapest and dearest slot on the horizon, with negative prices at the
  bottom of the scale where they belong, and `│` marking midnight. It reads like a
  price curve and needs nothing installed.
- **Every row is renamed.** Home Assistant builds a friendly name by joining the
  device name to the entity name, so left alone every row on the page would read
  "AI ESS Controller Planned action" and anything laid out in columns would
  truncate to "AI ESS Controll...". Each entity gets a short name of its own, and
  the section heading above it carries the context instead. Rename any of them
  and your name is what the dashboard shows.
- **It becomes yours, and it still gets better.** It is an ordinary storage
  dashboard: edit it, rearrange it, delete it. Every generated copy is stamped
  with a hash of itself, so an upgrade can tell its own untouched output from a
  dashboard you have since changed: untouched ones are refreshed to the new
  layout, edited ones are never written to again. A deleted dashboard stays
  deleted; the **Rebuild dashboard** button brings it back.
- **It degrades.** Cards are built from the entities that resolved, so disabling
  entities produces a smaller dashboard rather than a broken one.

Turn it off before setup with **Add a dashboard to the sidebar** in the optimiser
step if you would rather start from nothing.

### Building your own

Everything on the prebuilt dashboard is an ordinary entity, and the interesting
detail is in attributes rather than hidden: `slots` on the plan sensor is the
full half-hourly plan, ready for ApexCharts; the weekly report is the complete
summary dict; `placements` and `advice` carry the load schedule.

For a starting point rather than a blank page:

```yaml
action: ess_controller.generate_dashboard
data:
  write_file: true
```

That returns the same configuration as YAML — paste it into any dashboard's raw
configuration editor — and writes `config/ess_controller/dashboard.yaml`.

There is no public API for an integration to add a dashboard to the sidebar, so
this registers a panel and hands Lovelace its own storage object to serve the
configuration from. Every step is feature-detected and none of it is
load-bearing: if a Home Assistant release moves the furniture, the YAML is
written out, a **notification** tells you where and how to paste it in, and the
controller carries on regardless.

One consequence of that mechanism: the dashboard is not listed under
**Settings → Dashboards**, so there is no delete button for it. To remove it,
turn **Add a dashboard to the sidebar** off in the optimiser step. Your edits to
it are saved normally and survive restarts — and turning the option off keeps
them, so switching it back on restores what you had rather than the generated
version. **Rebuild dashboard** deliberately discards them and regenerates.

If it does not appear, check in this order:

1. **The version on disk.** The integration's device page shows its version. If
   it is not the version you just downloaded, HACS has not replaced the files or
   Home Assistant has not been restarted since. A reload is not enough — the old
   module stays in memory.
2. **The integration is actually configured.** A HACS download only copies files;
   until you add it under **Settings → Devices & services**, none of its code
   runs. Look for entities beginning `sensor.ess` in **Developer tools → States**.
3. **Notifications**, which is where the outcome is reported either way.
4. **Diagnostics** (**⋮ → Download diagnostics**) has a `dashboard` section
   giving the Lovelace data shape, how many entities resolved, how many views
   were built, and whether anything is registered at the URL path.

If entities exist but the dashboard has not appeared, the sidebar entry is
registered with a placeholder view that explains what to check — so an empty
"Starting up" page means the entities were not ready, not that registration
failed.

For the record, the two other mechanisms you may have seen are not available
here: **add-ons** (AdGuard Home, File editor, Terminal) get their sidebar entry
from the Supervisor's ingress, which only applies to add-ons; and **HACS** ships
a compiled JavaScript panel, which would mean writing a whole frontend and
giving up stock cards.

---

## Entities

**Sensors** — `grid_incentive_session`, `outage_risk`,
`scheduled_flexible_loads`, `tariff_recommendation`, `planned_action`,
`next_action`, `control_status`,
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
`exporting_planned`, `cheap_import_slot`, `grid_session_active`,
`free_electricity_now`, `outage_risk`, `flexible_load_scheduled_now`,
`control_active`, `inverter_available`, `plan_problem`,
`inverter_write_problem`.

**Controls** — `Optimiser enabled`, `Inverter control`, `Allow grid charging`,
`Allow export`, `Allow battery export`, `Act on grid sessions`,
`Shift flexible loads`, `Switch appliances`, `Outage protection`; numbers for the
SoC window, power limits,
wear allowance, typical daily load and the heating/cooling sensitivities; a
`Strategy` select; and buttons to re-plan, clear an override, compare tariffs,
rebuild the dashboard or reset learning.

**Services** — `ess_controller.generate_dashboard`, `export_performance`, `replan`, `set_override`, `clear_override`,
`reset_learning`, `recommend_tariffs`.

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

**Saving Session rewards are not always published.** When the supplier does not
expose a rate for a session, the configured fallback is used; with no fallback the
session is ignored rather than guessed at. Historic DFS rates have been well above
100p/kWh, so this is worth setting.

**Tariff comparison is a snapshot.** It scores a 24–48 hour window because that is
all Agile publishes. Treat it as "which tariff suits my system's shape", not a
guaranteed annual figure.

**Not tested against live hardware.** The logic has 410 automated tests,
including adapter tests against a simulated Solax entity set covering the
dropped-write failure mode. That is not the same as having run on a real
inverter. Start in advisory mode.

---

## Development

### Tests

```bash
pip install pytest ruff && python -m pytest tests/ -q
```

That runs without Home Assistant installed, which keeps it fast — and is why a
second job exists:

```bash
pip install pytest pytest-asyncio homeassistant   # needs Python 3.13
python -m pytest tests/test_with_homeassistant.py -q
```

Those boot a real instance and do what a user does: set the component up, walk
every config-flow step, open every options step, check the entities appear, and
register the dashboard. Three fatal bugs shipped because nothing did that — a
selector that could not be serialised (the config flow returned *400: Bad
Request*, so the integration could not be added at all), an import of a module
that does not exist (four platforms failed, so there were no entities), and a
deprecated coordinator call that stops working in Home Assistant 2026.8. None of
them are wrong as Python, so none of them were visible without importing Home
Assistant.

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
├── adjustments.py         # session repricing (Saving Sessions, Power Up)
├── shifting.py            # flexible load placement
├── performance.py         # recorded history and the metrics from it
├── dashboard.py           # the prebuilt dashboard's configuration
├── panel.py               # installing it into the sidebar
├── outage.py              # power cut anticipation
├── recommend.py           # tariff comparison
├── coordinator.py         # the planning loop
├── settings.py            # live-tunable settings (HA-free)
└── config_flow.py         # setup and options
```

## Licence

MIT
