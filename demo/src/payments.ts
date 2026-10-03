import express from "express";
import { pool, waitForDb } from "./db";
import { traced } from "./traced";

const FILE = "demo/src/payments.ts";
const app = express();
app.use(express.json());

export async function chargeCard(orderId: number, amountCents: number): Promise<number> {
  return traced("chargeCard", FILE, async () => {
    const r = await pool.query("INSERT INTO payments.charges(order_id, amount_cents) VALUES ($1, $2) RETURNING id", [
      orderId,
      amountCents,
    ]);
    return Number(r.rows[0].id);
  });
}

app.post("/charge", async (req, res) => {
  const { orderId, amountCents } = req.body ?? {};
  if (!Number.isInteger(orderId) || !Number.isInteger(amountCents)) return res.status(400).json({ error: "bad request" });
  res.json({ chargeId: await chargeCard(orderId, amountCents) });
});

app.get("/healthz", (_q, r) => void r.send("ok"));

waitForDb().then(() => app.listen(Number(process.env.PORT ?? 3003), () => console.log("payments listening")));
