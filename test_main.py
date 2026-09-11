import ast
from pathlib import Path
from main import boolean_match, Paper, merge_cross_source, SentStore

assert boolean_match('"amorphous alloy" AND ("machine learning" OR "data-driven")',
                     "Data-driven study of amorphous alloy")
assert boolean_match("recycling AND NOT polymer", "Metal recycling")
assert not boolean_match("recycling AND NOT polymer", "polymer recycling")

p1 = Paper(uid="x", title="A", authors=[], doi="10.1000/test")
p2 = Paper(uid="y", title="B", authors=[], doi="10.1000/test", abstract="richer")
merged = merge_cross_source([p1, p2])
assert len(merged) == 1
assert merged[0].abstract == "richer"

store_path = Path("data/test_sent_ids.json")
store = SentStore(store_path)
store.mark_many([p1])
store.save()
store2 = SentStore(store_path)
assert store2.contains(p2)
store_path.unlink(missing_ok=True)

print("All tests passed.")
