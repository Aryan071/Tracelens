import express from "express";
import { OrderController } from "../controllers/orderController";
import { requireAuth } from "../util/auth";

const router = express.Router();
const ctl = new OrderController();

router.post("/", requireAuth, async (req, res) => {
  res.status(201).json(await ctl.create(req.body));
});
router.get("/:id", (req, res) => ctl.get(req, res));
router.route("/:id/items").get(OrderController.listItems).delete(async (req, res) => {
  res.sendStatus(204);
});

export default router;
