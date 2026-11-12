# System design — question set

Default length: 5 questions, all on one design problem, from requirements to trade-offs.
Score areas (use these names exactly):

- `requirements`: asks for functional and non-functional requirements and the scale
- `high-level design`: components, data flow and APIs that fit the requirements
- `data model`: storage choice, schema, partitioning
- `scaling`: bottlenecks, caching, queues, replication, failure handling
- `trade-offs`: names alternatives and why one was chosen
- `communication`: structures the answer and checks in with the interviewer

Pick one problem that fits the role (adapt it to the posting's domain):

- a URL shortener with analytics
- a notification service (email, push, SMS) with retries and user preferences
- a rate limiter for a public API
- a job scheduler that runs millions of delayed tasks
- a document search service with near-real-time indexing
- a metrics ingestion pipeline with dashboards

Steps (one per reply):

1. The problem in two or three sentences. Ask the user for their requirements questions and assumptions;
   answer them briefly as the interviewer would.
2. The high-level design: main components and how a request flows.
3. The data model and storage choice.
4. Scale: traffic grows 100 times. Where does it break first, and what changes?
5. Failure: a component or a region goes down. What happens, and what are the trade-offs made?

Rubric: 5 senior-level, driven by requirements, trade-offs explicit; 4 solid with small gaps; 3 a working
design with hand-waving on scale or data; 2 major gaps or no structure; 1 no workable design.
