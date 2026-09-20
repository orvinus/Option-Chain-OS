# Broker comparison — explained in simple language

**A plain-language companion to the technical report of 19 September 2026.**

This document explains the same findings without any technical words. You do not
need to have read the main report to understand this one.

---

## First, three words explained

**Broker** — the company that actually places our buy and sell orders on the stock
exchange. Today that is Lakshmishree.

**API** — the doorway a broker provides so our software can place orders by itself,
instead of a person logging into a website and clicking buttons. Every broker has
one. They all do roughly the same job. They differ in **price**, in **reliability**,
and in **how honestly they explain themselves**.

**Lot** — options are not bought one share at a time. They are bought in fixed
bundles called lots. One NIFTY lot is 65 units. The trade we did was 13 lots.

---

## The whole thing in six lines

1. We compared **14 brokers**.
2. Every one of them is **fast enough**. Speed is not our problem.
3. Our broker charges us **about 26 times more** than a normal broker for the same trade.
4. Our software did not know this, because **no broker's system reports charges at all**.
5. The fix that is worth the most money is **a phone call**, not a software project.
6. If that call fails, the best replacement is **Fyers**.

---

## Finding 1 — Speed is not the problem

We measured how long it takes our computer to reach each broker's computer. Every
broker came back between **0.02 and 0.10 seconds**.

To put that in perspective: our trading system looks at the market and makes a
decision **once every minute**. A difference of five-hundredths of a second, inside
a sixty-second cycle, changes nothing at all.

**So speed is not a reason to pick or reject anyone.** All the advertising about
"ultra-fast" and "lowest latency" is irrelevant to the way we trade.

Two things in the speed test were still worth knowing:

- **Sharekhan is three to four times slower** than everybody else.
- **Kotak Neo's published address does not exist.** We typed in the address from
  their own instructions and the internet had no record of it.

And our current broker, Lakshmishree, was **among the fastest of all**. Whatever is
wrong, it is not their technology.

---

## Finding 2 — The cost is the problem, and it is very large

This is the most important page in the whole report.

Here is what the **exact same trade** — the real one we placed, 13 lots bought and
sold — would have cost at each broker:

| Broker | Cost of that one trade |
|---|---|
| Kotak Neo | **₹0** |
| Shoonya | **₹10** |
| Fyers, Groww, Zerodha, Dhan, Angel, Alice Blue, Jainam | **₹40** |
| Choice FinX | **₹650** |
| Sharekhan | **₹1,014** |
| **Lakshmishree — our broker today** | **₹1,040** |

### Why the gap is so enormous

It comes down to two small words in the price list.

Almost every broker charges **per order**. One flat fee, no matter how big the
trade. Buy and sell, that is two fees. Around ₹40 in total, finished.

Lakshmishree charges **per lot**. Our trade was **13 lots**, bought and then sold.
So that ₹40 fee was charged **26 separate times**.

Same trade. Same exchange. Same second. **₹40 at most brokers, ₹1,040 at ours.**

---

## Finding 3 — Why our software never noticed

Our system was calculating costs at ₹17 per order. The real charge was ₹1,040.

That is why our dashboard showed the trade losing about **₹210**, while the official
statement from the broker said we lost **₹1,398**.

Read that carefully, because it is the real lesson: **the trade itself was very
nearly break-even. The brokerage is what turned it into a loss.**

And here is why nobody could have caught it earlier by checking the software:

> **No broker's system reports charges. Not one rupee.**

The charges are buried inside the price shown on the printed contract note. We only
found the ₹40-per-lot figure by reading that paper document line by line and working
backwards. No amount of testing our own code would ever have revealed it.

---

## Finding 4 — What Lakshmishree promised, and what actually happened

You asked for this comparison specifically. Their official manual against our real
experience.

**Most of it is honest.** Orders go through. Positions match exactly — their records
said we held 845 units, ours said 845 units. Everything happens quickly.

**Four things do not match:**

1. **When our login expires, their computer replies "success."**
   This is the dangerous one. Any normal system reads "success" and carries on
   working — while actually being logged out and doing nothing. We discovered this
   during testing and fixed it. If we had not, live trading could have quietly
   stopped one morning with no warning and no error message.

2. **Their profit and loss figures never update.** They showed ₹0.00 the whole time,
   and the money-used figure lagged several minutes behind reality. We now calculate
   our own numbers and display them beside theirs.

3. **They never report charges** — covered above. This is the single biggest gap.

4. **Some money fields come back as the word "NaN"** instead of a number. Their
   documentation does not mention this anywhere.

**In short:** their technology is good. Their **reporting of money** is the problem.

---

## Finding 5 — Some brokers cannot explain themselves

This sounds like a minor point. It is not. If we cannot get clear instructions from
a broker, we cannot estimate how long connecting to them would take, and we cannot
predict what will break later.

- **Sharekhan** — their instruction page loads nothing at all. Their own price page
  for order limits literally reads "you will be able to place **XX** orders per second."
- **Choice FinX** — publishes **no instructions whatsoever**. We cannot even find out
  how to place an order with them.
- **Kotak Neo** — the address in their instructions is dead, and their software was
  replaced this month, so anything written about them online is already out of date.
- **"Sahi Pro"** — worth saying plainly: **this is not a broker.** It is a
  software-testing tool made by a completely different company. There is nothing here
  to connect to.
- **Jainam** — the link provided is **not** the same system we already use, although
  it looks similar at first glance. It is a different product of theirs.

**A broker we cannot understand is not a cheap broker. It is an unknown one.**

---

## Every broker in one line

| Broker | The short version |
|---|---|
| **Fyers** | ₹20 per order, free, clearly documented, runs unattended. **The safest replacement.** |
| **Shoonya** | Cheapest that actually works — ₹5 per order — and the fastest we measured. Needs a fixed internet address. |
| **Kotak Neo** | **Charges nothing.** But will not accept the order type our system uses, and their address is dead. |
| **Groww** | Best documentation of anyone, ₹20 per order. Their own instructions contradict themselves about the daily login. |
| **Dhan** | ₹20 per order. Fine. Needs a fixed internet address. |
| **Angel One** | ₹20 per order. Fine. Needs a fixed internet address. |
| **Alice Blue** | ₹20 per order. Fast, free, generous limits. |
| **Zerodha** | Good system, but **their rules require a person to log in by hand every morning.** We cannot use it. |
| **Jainam** | ₹20 per order. Different product from what we assumed. |
| **Choice FinX** | Fast and free, but no instructions exist and they block our order type. |
| **Sharekhan** | Slowest, ₹39 **per lot**, manual login, unreadable documentation. |
| **Lakshmishree** | Our broker. Fast and reliable — and **26 times too expensive.** |
| **Sahi Pro** | Not a broker at all. |

---

## What we should do

**Step one — make a phone call.**

Ask Lakshmishree to move us from "₹40 per lot" to "₹20 per order," which is what
every other broker in this list already offers.

That single conversation is worth roughly **₹1,000 on every trade we place**. It
costs nothing to ask, takes no development time, and carries no risk. Nothing else
in this entire report comes close to that.

**Step two — only if they refuse.**

Move to **Fyers**. They charge ₹20 per order, the connection is free, their
instructions are clear, and we have already worked out that connecting our system to
them would take roughly **six to eight working days**.

After Fyers, the alternatives are Shoonya (cheapest, but needs a fixed internet
address) and Groww (one question to settle first).

**Step three — a conversation, not a project.**

**Kotak Neo charges nothing**, which is genuinely the best price available. But they
do not allow the type of order our system sends; they silently change it into a
different type. That could mean we miss trades entirely. Worth one phone call to
confirm before anybody writes any code.

---

## The one sentence to remember

**Changing brokers is a project of a week or more. Negotiating the rate is a phone
call — and it saves almost the same money. Make the phone call first.**

---

*The full technical version, with all measurements, sources and per-broker detail,
is in `BROKER-API-COMPARISON-2026-09-19.pdf`.*
