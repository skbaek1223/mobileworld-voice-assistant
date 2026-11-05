// Evaluates the 늘밥 web prototype's mock_api.js on cases produced by test_neulbab_parity.py.
// usage: TZ=Asia/Seoul node neulbab_parity.js <app_dir> <cases.json> '<json list of words>'
"use strict";
const fs = require("fs");
const path = require("path");

const A = require(path.resolve(process.argv[2], "mock_api.js"));
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

const out = cases.map(function (c) {
  const s = c.settings, orders = c.orders, now = c.now;
  const ids = A.healthConditionsOf(s);
  const rec = A.recommend(A.MENUS, s, orders, now);
  const more = A.moreMenus(A.MENUS, s, orders, now);
  return {
    recommend: rec.map(function (m) { return m.id; }),
    more: more.map(function (m) { return m.id; }),
    reasons: rec.concat(more).map(function (m) { return A.reasonFor(m, s); }),
    healthNote: A.healthNoteText(s),
    parentHonorific: A.parentHonorific(s),
    guardianTitle: A.guardianTitle(s),
    menus: A.MENUS.map(function (m) {
      return {
        id: m.id,
        passes: A.passesFilter(m, s),
        health: A.healthResultLines(m, ids).map(function (r) { return r.text; }),
        nutrition: A.nutritionRows(m.id).map(function (r) { return r.text; })
      };
    }),
    orders: orders.map(function (o) {
      return {
        status: A.getStatus(o, now),
        byGuardian: A.orderedByText(o, s, "guardian"),
        byParent: A.orderedByText(o, s, "parent"),
        proxy: A.proxyNoticeText(o, s),
        eta: A.etaText(o),
        time: A.formatTime(o.createdAt),
        meal: A.mealWord(o.createdAt)
      };
    }),
    canGuardianOrder: A.canGuardianOrder(orders, now),
    notices: A.noticeList(orders, now, s).map(function (n) { return [n.orderId, n.status, n.at, n.text]; }),
    history: A.orderHistory(orders, now).map(function (h) { return [h.order.id, h.statusLabel, h.whenText]; })
  };
});
const words = JSON.parse(process.argv[4] || "[]");
const particles = words.map(function (w) { return [A.objParticle(w), A.subjParticle(w), A.dirParticle(w)]; });
process.stdout.write(JSON.stringify({ cases: out, particles: particles }));
