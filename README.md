# MegaVolt Energiegemeinschaft

An Austrian energy community that does not exist, run as carefully as one that does.

MegaVolt is a fictional **Bürgerenergiegemeinschaft** (BEG) — households, a few small
businesses and a municipality that produce electricity together and share it among themselves.

Every 15 minutes, the electricity the community generated is divided between its members in
proportion to what each of them actually used in that quarter hour, capped at what they used.
Whatever nobody uses is fed into the public grid. Whatever the community cannot cover is
bought from it. Do that 35,040 times a year, for every member, and get it right.

This project builds the machinery that makes the result fair and explainable:

1. **What did each member actually receive and pay in every 15-minute interval, and why?**
2. **Should a member stay on the fixed community price, or opt in to the dynamic price that
   follows the Austrian day-ahead market?** One is predictable, the other is usually cheaper.
   The honest answer depends on when that member uses electricity, and nobody knows that
   without looking at their profile.

Built on real public data from the ENTSO-E Transparency Platform (Austrian bidding zone,
`10YAT-APG------L`), with synthetic members — because a 15-minute consumption profile shows
when someone is home, and nobody publishes those.

The allocation rules modelled here are the public Austrian ones, described at
[energiegemeinschaften.gv.at](https://energiegemeinschaften.gv.at/messung-und-aufteilung/).

**Status:** phase 0 — foundations. ENTSO-E ingest works; the community model comes next.

A portfolio project by Mark Cvelfer Domadenik.
