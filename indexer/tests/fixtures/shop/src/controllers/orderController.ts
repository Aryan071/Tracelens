import { OrderService } from "../services/orderService";

export class OrderController {
  private svc = new OrderService();

  async create(body: unknown) {
    return this.svc.place(body);
  }

  get(req: unknown, res: unknown) {
    return this.svc.find(req);
  }

  static listItems(req: unknown, res: unknown) {
    return OrderController.helper();
  }

  private static helper() {
    return [];
  }
}
