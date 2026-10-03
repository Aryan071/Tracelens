import express from "express";
import { pool, waitForDb } from "./db";
import { traced } from "./traced";

const FILE = "demo/src/inventory.ts";
const app = express();
app.use(express.json());

export async function reserveStock(sku: string, qty: number): Promise<boolean> {
  return traced("reserveStock", FILE, async () => {
    const r = await pool.query(
      "UPDATE inventory.stock SET qty = qty - $2 WHERE sku = $1 AND qty >= $2 RETURNING qty",
      [sku, qty]
    );
    return r.rowCount === 1;
  });
}

app.post("/reserve", async (req, res) => {
  const { sku, qty } = req.body ?? {};
  if (typeof sku !== "string" || !Number.isInteger(qty) || qty < 1) return res.status(400).json({ error: "bad request" });
  const ok = await reserveStock(sku, qty);
  res.status(ok ? 200 : 409).json({ reserved: ok });
});

app.get("/healthz", (_q, r) => void r.send("ok"));

waitForDb().then(() => app.listen(Number(process.env.PORT ?? 3002), () => console.log("inventory listening")));
