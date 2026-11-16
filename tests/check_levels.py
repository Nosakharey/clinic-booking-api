# Usage: API_KEY=your-key python tests/check_levels.py https://your-api-url
# WARNING: it calls POST /reset, which wipes all bookings and slots.
import concurrent.futures as cf, httpx, uuid, time, sys
B = sys.argv[1]; import os; K = {"X-API-Key": os.getenv("API_KEY", "test-key")}
c = httpx.Client(base_url=B, headers=K, timeout=30)
ok = True
def check(name, cond, extra=""):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name, extra if not cond else "")
future = ["2027-01-10T09:00:00Z","2027-01-10T09:30:00Z"]
slots = [{"id":"s2","provider":"Dr Ade","start":"2027-01-10T10:00:00Z","end":"2027-01-10T10:30:00Z"},
         {"id":"s1","provider":"Dr Ade","start":future[0],"end":future[1]},
         {"id":"s3","provider":"Dr Bisi","start":"2027-01-11T09:00:00Z","end":"2027-01-11T09:30:00Z"},
         {"id":"past","provider":"Dr Ade","start":"2020-01-01T09:00:00Z","end":"2020-01-01T09:30:00Z"}]
slots += [{"id":f"c{i}","provider":"Dr C","start":f"2027-02-{i+1:02d}T09:00:00Z","end":f"2027-02-{i+1:02d}T09:30:00Z"} for i in range(15)]
r=c.post("/reset",json={"slots":slots}); check("reset 200", r.status_code==200, r.text)
# L1
r=c.get("/slots"); s=r.json()["slots"]; check("slots sorted", [x["id"] for x in s][:3]==["past","s1","s2"], [x["id"] for x in s][:4])
check("available flag", all(x["available"] is True for x in s))
check("provider filter", {x["id"] for x in c.get("/slots",params={"provider":"Dr Bisi"}).json()["slots"]}=={"s3"})
r=c.post("/bookings",json={"slot_id":"s1","name":"  Ada Obi ","email":" ADA@Example.com "}); b=r.json()
check("book 201", r.status_code==201 and b["status"]=="confirmed" and b["email"]=="ada@example.com" and b["name"]=="Ada Obi", r.text)
check("get booking", c.get(f"/bookings/{b['id']}").json()==b)
check("get 404", c.get("/bookings/nope").status_code==404 and "error" in c.get("/bookings/nope").json())
check("slot now unavailable", [x for x in c.get("/slots").json()["slots"] if x["id"]=="s1"][0]["available"] is False)
# L2
check("401 no key", httpx.get(B+"/slots").status_code==401 and httpx.get(B+"/slots").json().get("error"))
check("401 wrong key", httpx.get(B+"/slots",headers={"X-API-Key":"x"}).status_code==401)
for body,name in [({"name":"A","email":"a@b.co"},"missing slot"),({"slot_id":"s2","name":"  ","email":"a@b.co"},"blank name"),
                  ({"slot_id":"s2","name":"A","email":"not-an-email"},"bad email"),({"slot_id":"s2","name":"A"},"missing email"),
                  ({"slot_id":"s2","name":5,"email":"a@b.co"},"name not string")]:
    r=c.post("/bookings",json=body); check("400 "+name, r.status_code==400 and "error" in r.json(), r.text)
r=c.post("/bookings",content=b"not json"); check("400 bad json", r.status_code==400, r.text)
check("404 unknown slot", c.post("/bookings",json={"slot_id":"zz","name":"A","email":"z1@b.co"}).status_code==404)
check("400 past slot", c.post("/bookings",json={"slot_id":"past","name":"A","email":"z2@b.co"}).status_code==400)
# L3
check("409 taken", c.post("/bookings",json={"slot_id":"s1","name":"B","email":"b@b.co"}).status_code==409)
def bk(i): return c.post("/bookings",json={"slot_id":"s2","name":"X","email":f"r{i}@b.co"}).status_code
with cf.ThreadPoolExecutor(30) as ex: codes=list(ex.map(bk,range(30)))
check("concurrent: exactly one 201", codes.count(201)==1 and codes.count(409)==29, codes)
r=c.delete(f"/bookings/{b['id']}"); check("cancel 200", r.status_code==200 and r.json()["status"]=="cancelled", r.text)
check("cancel again 409", c.delete(f"/bookings/{b['id']}").status_code==409)
check("cancel 404", c.delete("/bookings/nope").status_code==404)
check("slot freed", [x for x in c.get("/slots").json()["slots"] if x["id"]=="s1"][0]["available"] is True)
r=c.post("/bookings",json={"slot_id":"s1","name":"C","email":"c@b.co"}); check("rebook after cancel 201", r.status_code==201, r.text)
# L4
k=str(uuid.uuid4()); body={"slot_id":"s3","name":"Idem","email":"idem@b.co"}
r1=c.post("/bookings",json=body,headers={"Idempotency-Key":k}); r2=c.post("/bookings",json=body,headers={"Idempotency-Key":k})
check("idem same response", r1.status_code==201 and r2.status_code==201 and r1.json()["id"]==r2.json()["id"], (r1.text,r2.text))
check("idem one booking", len(c.get("/bookings",params={"email":"idem@b.co"}).json()["bookings"])==1)
check("idem 422", c.post("/bookings",json={**body,"name":"Other"},headers={"Idempotency-Key":k}).status_code==422)
k2=str(uuid.uuid4()); body2={"slot_id":"c14","name":"Par","email":"par@b.co"}
with cf.ThreadPoolExecutor(10) as ex: rs=list(ex.map(lambda _: c.post("/bookings",json=body2,headers={"Idempotency-Key":k2}),range(10)))
check("idem concurrent same id", len({x.json().get("id") for x in rs})==1 and all(x.status_code==201 for x in rs), [x.status_code for x in rs])
check("idem concurrent one booking", len(c.get("/bookings",params={"email":"par@b.co"}).json()["bookings"])==1)
# pagination (5 attempts/min limit -> use several emails? use one email, 5 bookings)
for i in range(5): c.post("/bookings",json={"slot_id":f"c{i}","name":"P","email":"Page@b.co"})
got=[];cur=None
while True:
    p={"email":"PAGE@b.co","limit":2}; 
    if cur: p["cursor"]=cur
    j=c.get("/bookings",params=p).json(); got+= [x["slot_id"] for x in j["bookings"]]; cur=j["next_cursor"]
    if cur is None: break
check("pagination oldest first all", got==[f"c{i}" for i in range(5)], got)
check("default limit 10", c.get("/bookings",params={"email":"page@b.co"}).json()["next_cursor"] is None)
for bad in ["0","51","abc","-1","2.5"]:
    check(f"limit {bad} 400", c.get("/bookings",params={"email":"page@b.co","limit":bad}).status_code==400)
check("limit 50 ok", c.get("/bookings",params={"email":"page@b.co","limit":"50"}).status_code==200)
# L5
codes=[c.post("/bookings",json={"slot_id":"zz","name":"R","email":"spam@b.co"}).status_code for _ in range(6)]
r=c.post("/bookings",json={"slot_id":"zz","name":"R","email":"SPAM@b.co"})
check("5 attempts then 429", codes[:5]==[404]*5 and codes[5]==429, codes)
check("Retry-After int", r.status_code==429 and r.headers.get("Retry-After","").isdigit() and 1<=int(r.headers["Retry-After"])<=60, dict(r.headers))
check("other email unaffected", c.post("/bookings",json={"slot_id":"zz","name":"R","email":"other@b.co"}).status_code==404)
def spam(i): return c.post("/bookings",json={"slot_id":"zz","name":"R","email":"burst@b.co"}).status_code
with cf.ThreadPoolExecutor(20) as ex: cs=list(ex.map(spam,range(20)))
check("burst: exactly 5 pass limit", cs.count(404)==5 and cs.count(429)==15, cs)
# reset clears
c.post("/reset",json={"slots":slots}); check("reset clears bookings", c.get("/bookings",params={"email":"ada@example.com"}).json()["bookings"]==[])
check("web page public", httpx.get(B+"/").status_code==200)
print("ALL PASS" if ok else "SOME FAILED")
