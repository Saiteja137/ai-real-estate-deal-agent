# Interview notes

This application evaluates incoming wholesale property emails, presents a draft to a human, and uses an email approval before sending the frozen response. FastAPI serves a React dashboard; LangGraph coordinates extraction and evaluation. Deterministic Python calculations use Decimal. SMTP and IMAP are optional and disabled by default. Real provider integration has not been verified; tests mock external services.

1. **Why Decimal?** It avoids binary floating-point arithmetic during monetary calculations.
2. **What is MAO?** ARV multiplied by the configured ratio, less estimated repairs.
3. **What is gross spread?** ARV less asking price and repairs, before other costs.
4. **When is ROI unavailable?** When required costs are missing or the all-in cost is zero.
5. **Why preliminary?** Seller estimates have not been independently verified.
6. **Who classifies?** Deterministic Python rules.
7. **How is missing information handled?** Null values remain unknown.
8. **What does OpenAI do?** Optional structured extraction and draft generation with one function call.
9. **Can the model approve an email?** No; approval is processed by backend logic.
10. **What does LangGraph do?** It sequences extraction and evaluation.
11. **What survives restart?** Deal, approval and execution state in SQLite.
12. **Why hash the draft?** To detect changes to the frozen payload before execution.
13. **How are duplicate executions prevented?** A unique approval ID in executions and SQLite transactions.
14. **What happens after an edit?** Pending approvals are invalidated.
15. **How is an email reply checked?** Sender, confidential approval ID, decision, status, receipt time and payload hash are checked.
16. **What is an important identity limitation?** From addresses can be forged; an approval ID is a capability, not cryptographic proof of identity.
17. **How are uncertain SMTP outcomes handled?** They are marked for reconciliation and not automatically retried.
18. **How are offers ingested?** An optional IMAP worker uses a simple keyword filter and persists mailbox UIDs.
19. **What is unfinished?** Full model-callable tool coverage, normalized SQLAlchemy persistence and a complete audit timeline.
