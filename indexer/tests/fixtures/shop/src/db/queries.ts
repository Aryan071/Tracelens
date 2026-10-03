import { pool } from "./pool";

export async function insertOrder(o: unknown) {
  return pool.query("INSERT INTO orders (payload) VALUES ($1) RETURNING id", [o]);
}

export async function findOrder(id: unknown) {
  const sql = `SELECT id, payload FROM orders WHERE id = ${id}`;
  return pool.query(sql);
}
