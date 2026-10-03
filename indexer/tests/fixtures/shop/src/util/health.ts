export function health(req: unknown, res: { send(s: string): void }) {
  res.send("ok");
}
