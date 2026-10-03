import express from "express";
import orders from "./routes/orders";
import users = require("./routes/users");
import { health } from "./util/health";

const app = express();
app.use(express.json());
app.use("/api/orders", orders);
app.use("/api/users", users);
app.get("/healthz", health);

export default app;
