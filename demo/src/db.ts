import { Pool } from "pg";

export const pool = new Pool({
  connectionString: process.env.DATABASE_URL ?? "postgres://tracelens:tracelens@postgres:5432/demo",
  max: 10,
});

export async function waitForDb(retries = 30): Promise<void> {
  for (let i = 0; ; i++) {
    try {
      await pool.query("SELECT 1");
      return;
    } catch (e) {
      if (i >= retries) throw e;
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
}
