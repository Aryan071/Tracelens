// Steady load generator. RATE = requests/sec, DURATION_S = 0 means run forever.
const BASE = process.env.ORDERS_URL ?? "http://orders:3001";
const RATE = Number(process.env.RATE ?? 5);
const DURATION_S = Number(process.env.DURATION_S ?? 0);
const SKUS = ["sku-1", "sku-2", "sku-3", "sku-4", "sku-5"];
const rnd = (n: number) => Math.floor(Math.random() * n);

async function waitReady() {
  for (;;) {
    try {
      if ((await fetch(`${BASE}/healthz`)).ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
}

async function one() {
  const userId = 1 + rnd(100);
  const path = Math.random() < 0.7 ? "/orders" : `/users/${userId}/orders`;
  try {
    if (path === "/orders") {
      await fetch(BASE + path, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ userId, sku: SKUS[rnd(SKUS.length)], qty: 1 + rnd(3) }),
      });
    } else {
      await fetch(BASE + path);
    }
  } catch (e) {
    console.error("request failed", (e as Error).message);
  }
}

(async () => {
  await waitReady();
  const end = DURATION_S > 0 ? Date.now() + DURATION_S * 1000 : Infinity;
  while (Date.now() < end) {
    void one();
    await new Promise((r) => setTimeout(r, 1000 / RATE));
  }
})();
