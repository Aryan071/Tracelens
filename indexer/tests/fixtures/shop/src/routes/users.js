const express = require("express");
const { formatUser } = require("../util/format");
const router = express.Router();

router.get("/:id", function (req, res) {
  res.json(formatUser(req.params.id));
});

module.exports = router;
