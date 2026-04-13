노드(Node) 검증 — 5개
① 전체 노드 종류별 수량 확인
MATCH (n)
RETURN labels(n)[0] AS 레이블, count(n) AS 수량
ORDER BY 수량 DESC
Project 1, JavaFile N, Class M, Interface K, Method P, Field Q, Annotation R 형태로 나와야 합니다.

② JavaFile 노드 — mes4u 프로젝트 파일 목록 확인
MATCH (f:JavaFile)
RETURN f.fileName AS 파일명, f.packageName AS 패키지, f.path AS 경로
ORDER BY f.packageName, f.fileName
LIMIT 20
path가 C:\ai_test\mes4u\... 경로를 포함하고 있어야 합니다.

③ Class/Interface 노드 — 패키지별 클래스 수량
MATCH (c)
WHERE c:Class OR c:Interface
RETURN c.packageName AS 패키지,
       count(CASE WHEN c:Class THEN 1 END) AS 클래스수,
       count(CASE WHEN c:Interface THEN 1 END) AS 인터페이스수
ORDER BY 클래스수 DESC

④ Method 노드 — 복잡도 높은 메서드 TOP 10
MATCH (m:Method)
WHERE m.cyclomaticComplexity IS NOT NULL
RETURN m.classFqn AS 클래스,
       m.name AS 메서드,
       m.cyclomaticComplexity AS CC,
       m.loc AS LOC,
       m.visibility AS 접근제어자
ORDER BY m.cyclomaticComplexity DESC
LIMIT 10
CC가 10 이상이면 리팩토링 대상 후보입니다.

⑤ 고아 노드(관계 없는 노드) 확인
MATCH (n)
WHERE NOT (n)--()
  AND NOT n:Project
RETURN labels(n)[0] AS 레이블, n.name AS 이름, n.id AS id
LIMIT 20
결과가 비어 있어야 정상입니다. 관계가 누락된 노드가 있으면 여기서 잡힙니다.

관계(Relation) 검증 — 5개
① 전체 관계 종류별 수량 확인
MATCH ()-[r]->()
RETURN type(r) AS 관계타입, count(r) AS 수량
ORDER BY 수량 DESC
DECLARES, HAS_METHOD, HAS_FIELD, EXTENDS, IMPLEMENTS, DEPENDS_ON, CALLS, ANNOTATED_WITH 가 모두 나와야 합니다.

② DECLARES 관계 — 파일→클래스 연결 샘플 확인
MATCH (f:JavaFile)-[:DECLARES]->(c)
WHERE c:Class OR c:Interface
RETURN f.fileName AS 파일,
       labels(c)[0] AS 종류,
       c.name AS 클래스명,
       c.packageName AS 패키지
ORDER BY f.fileName
LIMIT 20
모든 JavaFile에 DECLARES 관계가 있어야 합니다.

③ HAS_METHOD 관계 — 클래스별 메서드 수 확인
MATCH (c)-[:HAS_METHOD]->(m:Method)
WHERE c:Class OR c:Interface
RETURN c.name AS 클래스,
       c.packageName AS 패키지,
       count(m) AS 메서드수
ORDER BY 메서드수 DESC
LIMIT 15
메서드 수가 0인 클래스가 있다면 HAS_METHOD 누락을 의심합니다.

④ CALLS 관계 — 메서드 호출 체인 샘플 확인
MATCH (caller:Method)-[r:CALLS]->(callee:Method)
RETURN caller.classFqn AS 호출클래스,
       caller.name AS 호출메서드,
       callee.classFqn AS 대상클래스,
       callee.name AS 대상메서드,
       r.call_line AS 호출라인
ORDER BY 호출라인
LIMIT 20
⑤ EXTENDS / IMPLEMENTS 관계 — 상속·구현 구조 확인
MATCH (child)-[r]->(parent)
WHERE type(r) IN ['EXTENDS', 'IMPLEMENTS']
RETURN type(r) AS 관계,
       child.name AS 자식클래스,
       parent.name AS 부모,
       child.packageName AS 패키지
ORDER BY 관계, 자식클래스
LIMIT 20

전체 정합성 한방 확인 쿼리

MATCH (f:JavaFile)
WHERE NOT (f)-[:DECLARES]->()
RETURN '⚠ DECLARES 누락 JavaFile' AS 경고, f.fileName AS 파일
UNION ALL
MATCH (c:Class)
WHERE NOT ()-[:DECLARES]->(c)
RETURN '⚠ DECLARES 없는 Class' AS 경고, c.name AS 파일
UNION ALL
MATCH (c:Class)
WHERE NOT (c)-[:HAS_METHOD]->()
  AND NOT (c)-[:HAS_FIELD]->()
RETURN '⚠ 메서드·필드 모두 없는 Class' AS 경고, c.name AS 파일