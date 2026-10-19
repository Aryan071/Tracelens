import express from "express";
import { pool, waitForDb } from "./db";
import { traced } from "./traced";

const FILE = "demo/src/orders.ts";
const INVENTORY = process.env.INVENTORY_URL ?? "http://inventory:3002";
const PAYMENTS = process.env.PAYMENTS_URL ?? "http://payments:3003";
const app = express();
app.use(express.json());

async function post(url: string, body: unknown): Promise<{ status: number; json: any }> {
  const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  return { status: r.status, json: await r.json() };
}

export async function insertOrder(userId: number, sku: string, qty: number): Promise<number> {
  return traced("insertOrder", FILE, async () => {
    const r = await pool.query("INSERT INTO orders.orders(user_id, sku, qty) VALUES ($1, $2, $3) RETURNING id", [userId, sku, qty]);
    return Number(r.rows[0].id);
  });
}

export async function findOrdersByUser(userId: number) {
  return traced("findOrdersByUser", FILE, async () => {
    const r = await pool.query("SELECT id, sku, qty, created_at FROM orders.orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT 20", [userId]);
    return r.rows;
  });
}

export async function createOrder(userId: number, sku: string, qty: number) {
  return traced("createOrder", FILE, async () => {
    const reserve = await post(`${INVENTORY}/reserve`, { sku, qty });
    if (reserve.status !== 200) return { status: 409, body: { error: "out of stock" } };
    const orderId = await insertOrder(userId, sku, qty);
    await post(`${PAYMENTS}/charge`, { orderId, amountCents: qty * 1999 });
    return { status: 201, body: { orderId } };
  });
}

app.post("/orders", async (req, res) => {
  const { userId, sku, qty } = req.body ?? {};
  if (!Number.isInteger(userId) || typeof sku !== "string" || !Number.isInteger(qty) || qty < 1) {
    return res.status(400).json({ error: "bad request" });
  }
  const out = await createOrder(userId, sku, qty);
  res.status(out.status).json(out.body);
});

app.get("/users/:userId/orders", async (req, res) => {
  res.json(await findOrdersByUser(Number(req.params.userId)));
});

app.get("/healthz", (_q, r) => void r.send("ok"));

waitForDb().then(() => app.listen(Number(process.env.PORT ?? 3001), () => console.log("orders listening")));
