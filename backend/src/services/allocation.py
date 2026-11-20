"""Exact aliquot allocation.

Each pending transfer application is handed over in full: the quantities of the
portions assigned to it must add up to the application quantity exactly.  A
portion is physical and indivisible, so it can be assigned to at most one
application.  Among all feasible assignments we minimise the total number of
portions used, because every used portion is one more aliquot point a
receiving room has to run.

The planner is a branch-and-bound search (anytime): it returns the best plan
it found plus a flag telling whether the minimum was proven.  It is not
first-fit greedy; there are instances where the naive "take the largest
portion that still fits" order gets stuck although a valid allocation exists.
Equal portion sizes are grouped into counts so a plan with 120 unit portions
does not explode into subset enumeration.  `greedy()` is kept as a starting
upper bound and is exercised by the tests.
"""

import time


class AllocationError(Exception):
    """Raised when no feasible plan exists inside the configured budget."""


class Allocation:
    """Result of `allocate`: plan, optimality flag and search statistics."""

    def __init__(self, plan, optimal, nodes, aborted):
        self.plan = plan
        self.optimal = optimal
        self.nodes = nodes
        self.aborted = aborted

    def __bool__(self):
        return self.plan is not None

    def __eq__(self, other):
        return isinstance(other, Allocation) and (self.plan, self.optimal) == (other.plan, other.optimal)

    def __repr__(self):
        return f'Allocation(plan={self.plan!r}, optimal={self.optimal!r})'


def _sorted(items):
    """Sort (key, qty) descending by quantity, keeping input order as tie-break."""
    return sorted(
        ((qty, pos, key) for pos, (key, qty) in enumerate(items)),
        key=lambda row: (-row[0], row[1]),
    )


def greedy(portions, demands):
    """Naive largest-portion-first heuristic.

    portions: [(key, quantity), ...]
    demands:  [(key, quantity), ...]
    Returns {demand_key: [portion_key, ...]} or None when this heuristic gets
    stuck.  A None result does not prove that no allocation exists.
    """
    items = _sorted(portions)
    used = [False] * len(items)
    result = {}
    for demand_key, demand_qty in sorted(demands, key=lambda pair: -pair[1]):
        remaining = demand_qty
        chosen = []
        while remaining > 0:
            pick = None
            for idx, (qty, _pos, _key) in enumerate(items):
                if not used[idx] and qty <= remaining:
                    pick = idx
                    break
            if pick is None:
                return None
            used[pick] = True
            remaining -= items[pick][0]
            chosen.append(items[pick][2])
        result[demand_key] = chosen
    return result


def _groups(portions):
    """Group portion codes by quantity: values desc, codes in input order."""
    buckets = {}
    for pos, (key, qty) in enumerate(portions):
        buckets.setdefault(qty, []).append((pos, key))
    values = sorted(buckets, reverse=True)
    codes = [[key for _pos, key in buckets[value]] for value in values]
    return values, codes


def _option_counts(target, values, avail):
    """Yield count vectors summing to target, larger quantities first."""
    count_values = len(values)
    suffix = [0] * (count_values + 1)
    for idx in range(count_values - 1, -1, -1):
        suffix[idx] = suffix[idx + 1] + values[idx] * avail[idx]

    counts = [0] * count_values

    def walk(idx, remaining):
        if remaining == 0:
            yield tuple(counts)
            return
        if idx >= count_values:
            return
        if suffix[idx] < remaining:
            return
        value = values[idx]
        maximum = min(avail[idx], remaining // value)
        for take in range(maximum, -1, -1):
            left = remaining - take * value
            if left > suffix[idx + 1]:
                continue
            counts[idx] = take
            yield from walk(idx + 1, left)
        counts[idx] = 0

    yield from walk(0, target)


def allocate(portions, demands, node_limit=400_000, time_limit=2.0):
    """Return an Allocation or None when no plan exists at all.

    portions: [(key, quantity), ...]   each quantity >= 1
    demands:  [(key, quantity), ...]   each quantity >= 1

    Every demand must be covered exactly; unused portions are allowed.  The
    plan maps demand key -> list of assigned portion keys.  Ties are resolved
    deterministically by the input order of the portions.

    If the search budget runs out, the best feasible plan found so far is
    returned with optimal=False.  If no feasible plan was found before the
    budget ran out, AllocationError is raised: the caller must not claim
    "impossible" without a proof.
    """
    if not demands:
        return Allocation({}, True, 0, False)
    portions = list(portions)
    demands = list(demands)
    if any(qty <= 0 for _key, qty in portions) or any(qty <= 0 for _key, qty in demands):
        return None
    if sum(qty for _key, qty in demands) > sum(qty for _key, qty in portions):
        return None

    values, codes = _groups(portions)
    counts = tuple(len(group) for group in codes)
    ordered_demands = _sorted(demands)              # (qty, input_pos, key)
    demand_qty = [row[0] for row in ordered_demands]
    demand_keys = [row[2] for row in ordered_demands]
    count_demands = len(demand_qty)

    if min(demand_qty) < min(values):
        return None

    suffix_qty = [0] * (count_demands + 1)
    suffix_count = [0] * (count_demands + 1)
    for idx in range(count_demands - 1, -1, -1):
        suffix_qty[idx] = suffix_qty[idx + 1] + demand_qty[idx]
        suffix_count[idx] = suffix_count[idx + 1] + 1

    heuristic = greedy(portions, demands)
    best = {key: list(chosen) for key, chosen in heuristic.items()} if heuristic else None
    best_count = sum(len(chosen) for chosen in best.values()) if best else float("inf")

    nodes = [0]
    deadline = time.monotonic() + time_limit
    aborted = [False]
    seen = {}

    def capacity(avail):
        return sum(value * amount for value, amount in zip(values, avail))

    def lower_bound(di, avail):
        """Pieces needed if every remaining demand were packed perfectly.

        Each demand needs at least ceil(qty / largest leftover portion), and
        the remaining portions must hold the remaining quantity.
        """
        largest = next((value for value, amount in zip(values, avail) if amount), 0)
        if not largest:
            return float('inf')
        bound = 0
        for future in range(di, count_demands):
            bound += -(-demand_qty[future] // largest)
        return bound

    def search(di, avail, used, path):
        nonlocal best, best_count
        if aborted[0]:
            return
        nodes[0] += 1
        if nodes[0] > node_limit or (nodes[0] % 1024 == 0 and time.monotonic() > deadline):
            aborted[0] = True
            return
        if used + suffix_count[di] >= best_count:
            return
        if di == count_demands:
            best_count = used
            best = _realise(path, demand_keys, values, codes)
            return
        if capacity(avail) < suffix_qty[di]:
            return
        if used + lower_bound(di, avail) >= best_count:
            return

        state = (di, avail)
        previous = seen.get(state)
        if previous is not None and previous <= used:
            return
        seen[state] = used

        target = demand_qty[di]
        options = []
        option_limit = 4000
        for option in _option_counts(target, values, avail):
            options.append(option)
            if len(options) >= option_limit:
                aborted[0] = True
                break
            if len(options) % 256 == 0 and time.monotonic() > deadline:
                aborted[0] = True
                break
        if aborted[0]:
            return
        # Fewer pieces first finds good plans early; bigger quantities first
        # keeps the order deterministic.
        options.sort(key=lambda option: (sum(option), tuple(-take for take in option)))
        for option in options:
            pieces = sum(option)
            if used + pieces + suffix_count[di + 1] >= best_count:
                break
            next_avail = tuple(amount - take for amount, take in zip(avail, option))
            path.append(option)
            search(di + 1, next_avail, used + pieces, path)
            path.pop()
            if aborted[0]:
                return

    search(0, counts, 0, [])
    if best is None:
        if aborted[0]:
            raise AllocationError("分装方案搜索超出计算上限，请减少分装份数或申请数量后重试")
        return None
    return Allocation(best, not aborted[0], nodes[0], aborted[0])


def _realise(path, demand_keys, values, codes):
    """Turn count vectors into concrete portion codes (first-in, first-out)."""
    cursors = [0] * len(values)
    result = {}
    for demand_key, option in zip(demand_keys, path):
        chosen = []
        for idx, take in enumerate(option):
            if take:
                chosen.extend(codes[idx][cursors[idx]:cursors[idx] + take])
                cursors[idx] += take
        result[demand_key] = chosen
    return result
