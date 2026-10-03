import { BaseService } from "./base";
import { insertOrder, findOrder } from "../db";

export class OrderService extends BaseService {
  async place(body: unknown) {
    this.audit("place");
    this.validate(body);
    return insertOrder(body);
  }

  find(id: unknown) {
    return findOrder(id);
  }

  private validate(body: unknown) {
    return Boolean(body);
  }
}
