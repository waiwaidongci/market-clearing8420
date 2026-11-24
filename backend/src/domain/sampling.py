"""结算日餐券抽查：同一原核销连同隔日退扣合占一个名额。

财务按结算日抽查餐券。两账中同一原核销及其隔日退扣属于同一张券，只占一个名额；
校区和档口组分别设有最低抽查数。补账重算时已抽出的券继续占用当日名额，并要按
新批次重新核对覆盖；两账归属不一致的券仍占名额，但不抵最低抽查数。

本模块只做纯计算，不访问数据库；名额台账的持久化见 ``services.sampling``。
"""

from collections import defaultdict

from .analysis import COMPARE
from .spec import CAMPUS, CANTEEN, REDEEM

SATISFIED = "已补足"
OVER_BUDGET = "预算不足"
UNFILLABLE = "凑不齐"
WITHHELD = "暂不补抽"
SCOPE_ZONE = "校区"
SCOPE_GROUP = "档口组"
EXISTING = "已抽"
ADDED = "补抽"


def _ledgers(rows):
    ledgers = {CAMPUS: {}, CANTEEN: {}}
    for row in rows:
        ledgers[row["ledger"]][row["transaction_key"]] = row
    return ledgers


def _clusters(rows):
    """Map each original redemption to every row that belongs to it, in either ledger."""
    ledgers = _ledgers(rows)
    roots = {row["transaction_key"] if row["kind"] == REDEEM else row["original_key"] for row in rows}
    clusters = {}
    for root in roots:
        members = {}
        for ledger, entries in ledgers.items():
            keys = {root} if root in entries else set()
            keys.update(row["transaction_key"] for row in entries.values()
                        if row["kind"] != REDEEM and row["original_key"] == root)
            members[ledger] = {key: entries[key] for key in keys if key in entries}
        clusters[root] = members
    return clusters


def _attribution(members):
    """Zone, group, consistency and why a voucher may not count toward a minimum.

    A voucher counts toward a minimum unless the two ledgers attribute its original
    redemption to different zones or merchant groups. A one-sided voucher keeps the
    attribution of the side that posted it; it still occupies quota.
    """
    originals = [row for ledger in (CAMPUS, CANTEEN)
                 for row in members[ledger].values() if row["kind"] == REDEEM]
    if not originals:
        return None, None, False, "新批次未见原核销，不抵最低数"
    zone, group = originals[0]["zone"], originals[0]["merchant_group"]
    if any(row["zone"] != zone or row["merchant_group"] != group for row in originals):
        return zone, group, False, "两账归属不一致，不抵最低数"
    return zone, group, True, ""


def _discrepancies(members):
    """Count transaction keys where the two ledgers disagree or only one side posted."""
    count = 0
    for key in members[CAMPUS].keys() | members[CANTEEN].keys():
        left, right = members[CAMPUS].get(key), members[CANTEEN].get(key)
        if left is None or right is None:
            count += 1
        elif any(left[field] != right[field] for field, _ in COMPARE):
            count += 1
    return count


def _active(members, day):
    return any(row["posting_date"] == day for entries in members.values() for row in entries.values())


def _describe(key, members, day, source):
    zone, group, consistent, reason = _attribution(members)
    discrepancies = _discrepancies(members)
    active = _active(members, day)
    return {
        "transaction_key": key,
        "zone": zone,
        "merchant_group": group,
        "source": source,
        "in_batch": True,
        "active": active,
        "consistent": consistent,
        "counts": active and consistent,
        "discrepant": discrepancies > 0,
        "discrepancies": discrepancies,
        "reason": reason,
    }


def _targets(day_rows, zone_minimum, group_minimum):
    targets = [{"scope": SCOPE_ZONE, "name": name, "minimum": zone_minimum}
               for name in sorted({row["zone"] for row in day_rows})]
    targets += [{"scope": SCOPE_GROUP, "name": name, "minimum": group_minimum}
                for name in sorted({row["merchant_group"] for row in day_rows})]
    return targets


def _coverage(vouchers):
    zones, groups = defaultdict(int), defaultdict(int)
    for voucher in vouchers:
        if voucher["counts"]:
            zones[voucher["zone"]] += 1
            groups[voucher["merchant_group"]] += 1
    return zones, groups


def _types(candidates):
    buckets = defaultdict(list)
    for voucher in candidates:
        buckets[(voucher["zone"], voucher["merchant_group"])].append(voucher)
    types = []
    for pair in sorted(buckets):
        entries = sorted(buckets[pair], key=lambda item: (not item["discrepant"], item["transaction_key"]))
        types.append((pair, entries))
    return types


class _Flow:
    """Min-cost max-flow over integer capacities (SPFA shortest augmenting paths)."""

    def __init__(self, nodes):
        self.graph = [[] for _ in range(nodes)]

    def edge(self, source, target, capacity, cost):
        forward = [target, capacity, cost, len(self.graph[target])]
        backward = [source, 0, -cost, len(self.graph[source])]
        self.graph[source].append(forward)
        self.graph[target].append(backward)
        return forward

    def push(self, source, sink, wanted):
        nodes = len(self.graph)
        total = cost = 0
        while total < wanted:
            distance = [None] * nodes
            previous = [None] * nodes
            distance[source] = 0
            queue, queued = [source], {source}
            while queue:
                node = queue.pop(0)
                queued.discard(node)
                for index, (target, capacity, edge_cost, _) in enumerate(self.graph[node]):
                    if capacity <= 0:
                        continue
                    candidate = distance[node] + edge_cost
                    if distance[target] is None or candidate < distance[target]:
                        distance[target] = candidate
                        previous[target] = (node, index)
                        if target not in queued:
                            queue.append(target)
                            queued.add(target)
            if distance[sink] is None:
                break
            step, node = wanted - total, sink
            while node != source:
                parent, index = previous[node]
                step = min(step, self.graph[parent][index][1])
                node = parent
            node = sink
            while node != source:
                parent, index = previous[node]
                self.graph[parent][index][1] -= step
                reverse = self.graph[parent][index][3]
                self.graph[node][reverse][1] += step
                node = parent
            total += step
            cost += step * distance[sink]
        return total, cost


def _choice(types, zone_need, group_need):
    """Exact minimum voucher selection via min-cost flow with lower bounds.

    The primary objective is the fewest vouchers; ties prefer more discrepant
    vouchers, then transaction-key order. Returns the chosen vouchers or ``None``.
    """
    if not any(zone_need.values()) and not any(group_need.values()):
        return []
    zones = sorted(zone_need)
    groups = sorted(group_need)
    supply = defaultdict(int)
    for (zone, group), entries in types:
        supply[(SCOPE_ZONE, zone)] += len(entries)
        supply[(SCOPE_GROUP, group)] += len(entries)
    for name, need in zone_need.items():
        if supply[(SCOPE_ZONE, name)] < need:
            return None
    for name, need in group_need.items():
        if supply[(SCOPE_GROUP, name)] < need:
            return None

    vouchers = sorted((voucher for _, entries in types for voucher in entries),
                      key=lambda item: item["transaction_key"])
    total = len(vouchers)
    # Lexicographic objective encoded as one additive cost: fewest vouchers, then
    # most discrepant vouchers, then a stable preference for earlier keys.
    rank = {voucher["transaction_key"]: index for index, voucher in enumerate(vouchers)}
    discrepancy_unit = total * total + 1
    voucher_unit = (total + 1) * discrepancy_unit

    source, sink = 0, 1
    zone_node = {name: 2 + index for index, name in enumerate(zones)}
    group_node = {name: 2 + len(zones) + index for index, name in enumerate(groups)}
    super_source = 2 + len(zones) + len(groups)
    super_sink = super_source + 1
    flow = _Flow(super_sink + 1)
    balance = defaultdict(int)

    for name in zones:
        low, high = zone_need[name], supply[(SCOPE_ZONE, name)]
        flow.edge(source, zone_node[name], high - low, 0)
        balance[source] -= low
        balance[zone_node[name]] += low
    for name in groups:
        low, high = group_need[name], supply[(SCOPE_GROUP, name)]
        flow.edge(group_node[name], sink, high - low, 0)
        balance[group_node[name]] -= low
        balance[sink] += low
    chosen_edges = []
    for (zone, group), entries in types:
        for voucher in entries:
            cost = voucher_unit - (discrepancy_unit if voucher["discrepant"] else 0) \
                + rank[voucher["transaction_key"]]
            chosen_edges.append((voucher, flow.edge(zone_node[zone], group_node[group], 1, cost)))
    # Close the circulation so lower-bound supplies can be balanced.
    flow.edge(sink, source, sum(supply.values()) + 1, 0)

    demand = 0
    for node, value in balance.items():
        if value > 0:
            flow.edge(super_source, node, value, 0)
            demand += value
        elif value < 0:
            flow.edge(node, super_sink, -value, 0)
    pushed, _ = flow.push(super_source, super_sink, demand)
    if pushed != demand:
        return None
    chosen = [voucher for voucher, edge in chosen_edges if edge[1] == 0]
    chosen.sort(key=lambda item: item["transaction_key"])
    return chosen


def _gap_rows(targets, coverage, unfillable, over_budget):
    zones, groups = coverage
    rows = []
    for target in targets:
        covered = zones[target["name"]] if target["scope"] == SCOPE_ZONE else groups[target["name"]]
        shortfall = max(0, target["minimum"] - covered)
        if not shortfall:
            state = SATISFIED
        elif (target["scope"], target["name"]) in unfillable:
            state = UNFILLABLE
        elif over_budget:
            state = OVER_BUDGET
        else:
            state = WITHHELD
        rows.append({"scope": target["scope"], "name": target["name"], "minimum": target["minimum"],
                     "covered": covered, "shortfall": shortfall, "state": state})
    return rows


def _voucher_view(voucher):
    item = dict(voucher)
    item.pop("in_batch", None)
    if not voucher.get("in_batch", True):
        item["note"] = "新批次未见该券，照占名额"
    elif not voucher["active"]:
        item["note"] = "新批次当日未见该券，照占名额"
    elif not voucher["consistent"]:
        item["note"] = voucher["reason"]
    elif voucher["source"] == ADDED:
        item["note"] = "本次补抽"
    else:
        item["note"] = ""
    return item


def plan(rows, day, budget, zone_minimum, group_minimum, sampled=()):
    """Plan the settlement-day sample and report coverage against persisted draws.

    ``rows`` are validated ledger rows. ``sampled`` lists roots already drawn for the
    same settlement day: they keep occupying quota and are re-checked against ``rows``.
    """
    clusters = _clusters(rows)
    day_rows = [row for row in rows if row["posting_date"] == day]
    targets = _targets(day_rows, zone_minimum, group_minimum)

    sampled = list(dict.fromkeys(sampled))
    existing = []
    for key in sampled:
        if key in clusters:
            existing.append(_describe(key, clusters[key], day, EXISTING))
        else:
            existing.append({"transaction_key": key, "zone": None, "merchant_group": None,
                             "source": EXISTING, "in_batch": False, "active": False,
                             "consistent": False, "counts": False, "discrepant": False,
                             "discrepancies": 0})

    coverage = _coverage(existing)
    needs = {}
    for target in targets:
        scope, name = target["scope"], target["name"]
        covered = coverage[0][name] if scope == SCOPE_ZONE else coverage[1][name]
        needs[(scope, name)] = max(0, target["minimum"] - covered)
    zone_need = {name: needs[(SCOPE_ZONE, name)] for name in
                 {target["name"] for target in targets if target["scope"] == SCOPE_ZONE}}
    group_need = {name: needs[(SCOPE_GROUP, name)] for name in
                  {target["name"] for target in targets if target["scope"] == SCOPE_GROUP}}

    candidates = [_describe(key, members, day, ADDED)
                  for key, members in clusters.items()
                  if key not in sampled and _active(members, day) and _attribution(members)[2]]
    types = _types(candidates)

    supply = defaultdict(int)
    for (zone, group), entries in types:
        supply[(SCOPE_ZONE, zone)] += len(entries)
        supply[(SCOPE_GROUP, group)] += len(entries)

    occupied = len(sampled)
    remaining = max(0, budget - occupied)
    unfillable = {target_key for target_key, value in needs.items()
                  if value > supply[target_key]}
    chosen = []
    need = 0
    approximate = False
    if any(needs.values()) and not unfillable:
        chosen = _choice(types, zone_need, group_need)
        if chosen is None:
            # Individually reachable but not jointly: flag every unmet target.
            unfillable = {target_key for target_key, value in needs.items() if value}
            chosen = []
        need = len(chosen)
    elif unfillable:
        # Size the budget need from the targets that could still be filled.
        open_zones = {name: value for name, value in zone_need.items()
                      if (SCOPE_ZONE, name) not in unfillable}
        open_groups = {name: value for name, value in group_need.items()
                       if (SCOPE_GROUP, name) not in unfillable}
        open_types = [(pair, entries) for pair, entries in types
                      if pair[0] in open_zones and pair[1] in open_groups]
        if any(open_zones.values()) or any(open_groups.values()):
            sibling = _choice(open_types, open_zones, open_groups)
        else:
            sibling = []
        if sibling is None:
            need, approximate = max(sum(open_zones.values()), sum(open_groups.values())), True
        else:
            need = len(sibling)
    over_budget = need > remaining
    blocked = bool(unfillable) or over_budget
    drawn = [] if blocked else [voucher["transaction_key"] for voucher in chosen]

    if blocked:
        status = "blocked"
    elif drawn:
        status = "drawn"
    else:
        status = "satisfied"

    sampled_keys = {voucher["transaction_key"] for voucher in chosen} if not blocked else set()
    final = existing + [voucher for voucher in chosen if voucher["transaction_key"] in sampled_keys]
    gap_rows = _gap_rows(targets, _coverage(final), unfillable, over_budget)
    shortfall = sum(row["shortfall"] for row in gap_rows)

    labels = [f'{scope} {name}' for scope, name in sorted(unfillable)]
    if over_budget:
        wording = "至少" if approximate else ""
        message = (f"预算不足：本次需补抽{wording} {need} 张，当日剩余 {remaining} 张"
                   f"（已占 {occupied}/{budget}），暂不补抽，缺口 {shortfall} 张。")
        if unfillable:
            message += f" 另凑不齐的组：{'、'.join(labels)}。"
    elif unfillable:
        message = f"凑不齐的组：{'、'.join(labels)}；暂不补抽，缺口 {shortfall} 张。"
    elif drawn:
        message = (f"本次补抽 {len(drawn)} 张，覆盖已满足最低数"
                   f"（当日已占 {occupied + len(drawn)}/{budget} 张名额）。")
    elif existing:
        message = f"沿用当日已抽的 {occupied} 张券，覆盖已满足最低数。"
    else:
        message = "当日没有需要补抽的券。"

    return {
        "status": status,
        "budget": budget,
        "occupied": occupied,
        "remaining": remaining,
        "need": need,
        "drawn": drawn,
        "keys": [voucher["transaction_key"] for voucher in final],
        "vouchers": [_voucher_view(voucher) for voucher in final],
        "targets": gap_rows,
        "shortfall": shortfall,
        "unfillable": labels,
        "message": message,
        "zones": [target["name"] for target in targets if target["scope"] == SCOPE_ZONE],
        "groups": [target["name"] for target in targets if target["scope"] == SCOPE_GROUP],
    }
