export function checkQty(qty: number): number {
  if (qty > 3) {
    throw new Error("qty too large: " + qty);
  }
  return qty;
}

export async function createOrder(qty: number): Promise<number> {
  await new Promise((r) => setTimeout(r, 1));
  const total = checkQty(qty) * 2;
  return total;
}

async function main() {
  try {
    await createOrder(9);
  } catch (e) {
    console.log((e as Error).stack);
  }
}
main();
