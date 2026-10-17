# Memo: can telemetry tell us where to send the trucks?

To: Services Operations leadership
From: Arnav Chawla
Date: 2026-10-17
Repository: services-ops-intelligence, tag v0.1

## The question

Every day a field organisation decides where to send a limited number of
technicians. Some of those visits replace a part that was about to fail; some
roll for nothing. This memo answers three questions on one quarter of data:

1. If we can afford B truck rolls a day, how many of the drives that will fail
   in the next 30 days can a model put on that list, and how does that compare
   with the rule of thumb the industry already uses?
2. Can last week's parts usage be forecast one week ahead better than
   "same as last week"?
3. How often does a truck go back to the same asset, and how much of that is
   avoidable?

## The data

The hardware telemetry is real: Backblaze publishes a daily health snapshot
(SMART counters and a failure flag) for every hard drive in its data centres.
The 2026 second quarter holds 31,968,003 drive-days, 359,101 drives, 81 drive
models and 1,522 failures over 91 days (2026-04-01 to 2026-06-30).

Everything around the telemetry is generated: the 335 customer accounts, 701
sites, 8,281 support cases, 4,108 dispatches and 30 parts were produced by a
seeded program in this repository that places every real drive at a customer
site and opens cases around the real failure dates. The generated data is
deliberately messy in documented ways (one customer spelled three ways, dates
in two formats, two definitions of "failure"), and the program keeps the
ground truth, which is what makes the dispatch analysis checkable. Section
"What is real and what is generated" below says which numbers rest on which.

## What the model does

For each drive and each day t, the model gives the probability that the
drive fails within the next 30 days, using only what was known on day t: the
five SMART counters Backblaze found predictive (reallocated sectors, reported
uncorrectable errors, command timeouts, pending sectors, offline
uncorrectable), power-on hours, temperature, interface errors, bytes read and
written, the drive model and capacity, and how those counters moved over the
previous seven days.

Three rankings were compared:

- Backblaze's rule: flag any drive with any of the five counters above zero.
- A logistic regression.
- A gradient-boosted tree model (LightGBM), with probabilities calibrated on
  drives the model never trained on.

The test is strict about time. The models were trained on prediction dates
2026-04-08 to 2026-04-24, whose 30-day labels run to 2026-05-24. They were
then scored on 2026-05-25 to 2026-05-31, whose labels run to 2026-06-30. No
failure that counts in the test was ever a training label, and nothing was
tuned on the test window. The test holds 2,469,958 drive-days and 2,123
positive drive-days from 404 distinct drives that failed.

## The dispatch-budget result

On an average test day about 303 of the 353,000 drives are within 30 days of
failing. Backblaze's rule flags 29,930 drives a day; if every flagged drive
got a truck, 67% of the failures would be caught, at 0.7% precision. No
operation can roll 30,000 trucks, so the useful comparison is at a budget.

| Trucks per day | Rule: caught | Rule: for nothing | LightGBM: caught | LightGBM: for nothing | LightGBM share of the day's 303 |
|---:|---:|---:|---:|---:|---:|
| 10 | 0.1 | 9.9 | 5.6 | 4.4 | 1.8% |
| 25 | 1.1 | 23.9 | 10.6 | 14.4 | 3.5% |
| 50 | 2.4 | 47.6 | 16.9 | 33.1 | 5.6% |
| 100 | 7.9 | 92.1 | 27.9 | 72.1 | 9.2% |
| 250 | 20.4 | 229.6 | 50.6 | 199.4 | 16.7% |

In sentences: with 50 trucks a day, the model puts about 17 soon-to-fail
drives on the list and 33 trucks roll for nothing (one in three visits finds
a drive that fails within the month). The rule of thumb at the same budget
finds 2 or 3. With 100 trucks, the model finds 28 against the rule's 8. The
logistic regression sits between the two (9 at 50 trucks, 19 at 100).

The honest reading of the same table: even the best ranking catches under 10%
of the coming month's failures with 100 trucks a day, because 303 drives a day
are failing and most of them show no warning in their counters. The model is
good at ordering the queue, not at making failures predictable. Ranking
quality on the test window: precision-recall area 0.099 for LightGBM, 0.057
for the logistic regression, 0.018 for the rule (the floor, random ordering,
is 0.0009). Of all failures, 50% are in the model's top 1% of drives and 21%
in its top 0.1%.

The calibrated probabilities are usable but rough: the model's top decile
predicts 0.81% and observes 0.61%; the bottom deciles under-predict slightly.
Calibration rests on 415 failure events, and the whole test on 404 failing
drives, so the numbers above move by a few points between reasonable choices
of window. They should be read as "the model is several times better than the
rule at any budget", not as "16.9".

## Parts demand

Weekly parts usage per region and part was forecast one week ahead over the
last six weeks of the quarter (13 weeks exist, so a monthly backtest was not
possible). Average absolute error per region-and-part series, where the
average week uses 1.2 units: last-week naive 0.84, four-week average 0.79,
exponential smoothing 0.73, pooled LightGBM 0.68. Summed to fleet totals per
part, exponential smoothing was best (2.00 against 2.37 for naive) and the
LightGBM under-forecast as demand fell through June. On this data a simple
smoother beats "same as last week" by about 15%; the learned model buys a
little more at the series level and nothing at the total. This demand was
generated from the real failure dates plus random background cases, so the
pipeline and the comparison are proven, the demand itself is not.

## Repeat dispatches

A repeat is a second or third visit on the same case, or a first visit within
30 days of a previous dispatched case closing on the same asset. Of 4,108
dispatches, 1,138 (27.7%) are repeats: 1,001 on the same case and 137 across
cases. Against the ground truth, 472 of the repeats were necessary
(follow-ups after a part was missing or a visit ran out of time on a real
fault) and 666 were avoidable. Drive-failure cases have a high repeat rate
(29%) but a low avoidable rate (11.5%); configuration, how-to and firmware
cases have repeat rates of 23% to 30% with every repeat avoidable, because
those cases had no hardware fault to begin with. Forty percent of first visits
were unnecessary by the same truth. These rates reproduce the rules the
generator was given; they describe the method, not a real operation.

## What is real and what is generated

Real: the telemetry, the failure labels, the failure model, every number in
the dispatch-budget table, the ranking metrics, and the drive-level risk list.
Generated: which customer owns which drive, the cases, the dispatches, the
parts used, and therefore every number in the parts and repeat-dispatch
sections. The generated numbers test that the code answers the question
correctly (the ground truth is known); they do not describe any customer.

## What would change with a year of data

- The test would cover several months instead of seven days and several
  thousand failures instead of 404, so the budget table would have error bars
  worth printing and calibration would be trustworthy per drive model.
- Seasonal and monthly backtests of parts demand become possible, and the
  forecast could be tied to the failure model (expected failures by site next
  month, hence sleds to stage at each depot).
- Repeat-dispatch rates by technician and supplier would stop being noise:
  most technicians have fewer than 60 visits in this quarter.
- Real case and dispatch records would replace the generator, and the
  "avoidable" split would need a human-reviewed sample instead of ground truth.
