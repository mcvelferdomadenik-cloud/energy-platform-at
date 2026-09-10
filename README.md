# MegaVolt

An Austrian electricity supplier that does not exist, run as carefully as one that does.

MegaVolt supplies electricity to households and small businesses that are members of a
**Bürgerenergiegemeinschaft** — a citizen energy community whose members generate electricity
together and share it among themselves. MegaVolt is a participant in that community, which
Austrian law allows for this type of community and not for the renewable one.

That makes MegaVolt a supplier with an unusually hard job.

Every 15 minutes, the community first divides its own generation between its members, in
proportion to what each of them actually used. Only what is left uncovered is MegaVolt's to
supply. So MegaVolt does not sell consumption — it sells **the remainder of consumption after
the community has taken its share**, and that remainder is much harder to forecast than plain
demand. On a sunny afternoon its customers' offtake collapses, not because they use less, but
because the community already covered them.

MegaVolt has to buy that volume on the day-ahead market before it knows any of this. Every
15-minute interval where the guess was wrong is billed by the grid operator as imbalance.

This project builds the machinery that keeps that business honest and explainable:

1. **What did each customer actually cost us, per 15-minute interval, and why?**
   Buy the wrong volume and the difference is settled at the imbalance price, which is where a
   supplier quietly loses money.
2. **What should we offer this customer — a fixed price, or the opt-in dynamic tariff that
   follows the market?** One is predictable, the other is usually cheaper. Which one is right
   depends on when that customer uses electricity and how much of it the community already
   covers, and nobody knows that without looking at the profile.

Built on real public data from the ENTSO-E Transparency Platform (Austrian bidding zone,
`10YAT-APG------L`), with synthetic customers — because a 15-minute consumption profile shows
when someone is home, and nobody publishes those.

The energy community rules modelled here are the public Austrian ones, described at
[energiegemeinschaften.gv.at](https://energiegemeinschaften.gv.at/messung-und-aufteilung/).

**Status:** phase 0 — foundations. ENTSO-E ingest works; the settlement model comes next.

A portfolio project by Mark Cvelfer Domadenik.
