# Ingestion 파이프라인 구조 설명

Java 소스코드를 읽어서 Neo4j 그래프 DB에 적재하는 전체 흐름을 설명합니다.

---

## 전체 흐름 한눈에 보기

```
Java 소스 파일들
      ↓
[STEP 1] SourceCollector      — .java 파일 수집 (파일경로 → 소스코드 딕셔너리)
      ↓
[STEP 2] IncrementalTracker   — 변경된 파일만 필터링 (SHA-256 해시 비교)
      ↓
[STEP 3] ASTParser            — 소스코드 → ClassInfo 객체 (클래스/메서드/필드 구조화)
      ↓
[STEP 4] ComplexityAnalyzer   — 각 메서드에 복잡도 메트릭 추가
      ↓
[STEP 5] GraphModelMapper     — ClassInfo[] → 노드/관계 딕셔너리 변환
      ↓
[STEP 6] Neo4jLoader          — Neo4j에 배치 MERGE 적재
```

각 STEP은 독립적인 클래스로 구현되어 있고, `ingestion/main.py`가 이를 순서대로 호출합니다.

---

## 데이터 구조 (models/code_info.py)

파이프라인 내부에서 데이터를 주고받는 핵심 객체들입니다.

```
ClassInfo                         ← 클래스 하나를 표현
├── file_path  : 파일 경로
├── package    : 패키지명
├── class_name : 클래스명
├── fqn        : 완전한 이름 (com.example.OrderService)
├── extends    : 부모 클래스명
├── implements : 구현 인터페이스 목록
├── is_interface / is_abstract / is_final
├── fields     : { 변수명 → 타입명 }  dict
└── methods    : { 메서드명 → MethodInfo }  dict

MethodInfo                        ← 메서드 하나를 표현
├── name, return_type, params
├── visibility : public/protected/private/package
├── is_static
├── start_line, end_line
├── source_snippet : 메서드 소스코드 원문
├── id         : FQN (com.example.OrderService#createOrder)
├── signature  : createOrder(Order):void
├── calls      : [ CallInfo ]     ← 이 메서드가 호출하는 다른 메서드들
└── 복잡도 메트릭: cyclomatic_complexity, cognitive_complexity, loc, ...

CallInfo                          ← 메서드 호출 관계 하나를 표현
├── callee_class  : 호출 대상 클래스명
├── callee_method : 호출 대상 메서드명
└── line          : 호출 위치 라인

GraphData                         ← Neo4j 적재 직전 형태
├── nodes : [ { label, id/fqn, ...속성 } ]
└── edges : [ { type, from_id, to_id, from_label, to_label } ]
```

---

## STEP 1 — SourceCollector (collector/source_collector.py)

**역할**: 지정한 디렉토리에서 `.java` 파일을 전부 읽어 딕셔너리로 반환합니다.

```python
# 반환 형태
{
  "src/main/java/com/example/OrderService.java": "public class OrderService { ... }",
  "src/main/java/com/example/Order.java": "public class Order { ... }",
  ...
}
```

**주요 동작**:
- Maven/Gradle 표준 구조(`src/main/java`)를 자동 감지하여 탐색 루트 결정
- `include_test=False`(기본값)이면 `src/test` 경로 제외
- 파일 읽기 실패 시 해당 파일만 건너뜀 (전체 중단 없음)

**설정 연결**: `CollectorConfig.base_path`, `include_test`, `file_encoding`

---

## STEP 2 — IncrementalTracker (collector/incremental_tracker.py)

**역할**: 지난번 실행 이후 **변경된 파일만** 추려서 불필요한 재파싱을 방지합니다.

```
처음 실행      → 저장된 해시 없음 → 전체 파일 파싱
파일 수정 후   → 해시 달라짐     → 해당 파일만 재파싱
파일 삭제 후   → 현재 목록에 없음 → Neo4j에서 해당 노드 삭제
파일 변경 없음  → 해시 동일      → 완전히 건너뜀 (파싱 비용 0)
```

**동작 원리**:
1. Neo4j의 `:JavaFile` 노드에서 `{ path → contentHash }` 맵을 읽어옴
2. 현재 수집한 파일의 SHA-256 해시와 비교
3. 해시가 다른 파일만 반환 → 이후 STEP에서 처리
4. Neo4j에 있지만 현재 소스에 없는 파일은 `DETACH DELETE`로 삭제

> 해시는 Neo4j `:JavaFile` 노드의 `contentHash` 속성에 저장됩니다 (적재 시 함께 저장).

---

## STEP 3 — ASTParser (parser/ast_parser.py)

**역할**: Java 소스코드를 AST(추상 구문 트리)로 파싱하여 `ClassInfo` 객체로 변환합니다.

내부적으로 `javalang` 라이브러리가 Java 소스를 트리 구조로 분해하고, ASTParser가 이 트리에서 필요한 정보를 추출합니다.

**추출하는 정보**:

| 대상 | 추출 내용 |
|------|----------|
| 클래스 | 이름, 패키지, extends, implements, 어노테이션, abstract/final 여부 |
| 필드 | 변수명 → 타입명 매핑 (나중에 메서드 호출 대상 해석에 사용) |
| 메서드 | 이름, 반환타입, 파라미터, 접근제어자, 소스코드 원문, 시작/종료 라인 |
| 호출관계 | 메서드 내 `객체.메서드()` 호출 → `CallInfo` 목록 |

**호출관계 추출 방식**:
```java
// Java 코드
this.orderRepo.save(order);   // qualifier=orderRepo, member=save
```
```python
# field_map = { "orderRepo": "OrderRepository" }
# qualifier "orderRepo" → 실제 타입 "OrderRepository" 로 해석
# → CallInfo(callee_class="OrderRepository", callee_method="save")
```
필드 선언의 타입 정보(`field_map`)를 활용해서 `qualifier`(변수명)를 실제 클래스명으로 변환합니다.

**파싱 실패 처리**: Java 14+ 신문법(record, sealed 등) 파싱 실패 시 `parse_failures.log`에 기록하고 해당 파일만 건너뜁니다.

---

## STEP 4 — ComplexityAnalyzer (parser/complexity_analyzer.py)

**역할**: 각 메서드의 소스코드 원문을 분석하여 복잡도 메트릭을 계산합니다.

**계산하는 메트릭**:

| 메트릭 | 설명 | 권장 기준 |
|--------|------|----------|
| `cyclomatic_complexity` (CC) | 분기 경로 수. `if/for/while/case/catch/&&/||` 마다 +1 | 10 이하 권장 |
| `cognitive_complexity` (CogC) | 중첩 깊이를 반영한 복잡도. 중첩이 깊을수록 가중치 ↑ | 15 이하 권장 |
| `loc` | 실행 코드 라인 수 (빈줄·주석 제외) | 20 이하 권장 |
| `param_count` | 파라미터 개수 | 4 이하 권장 |
| `fan_out` | 이 메서드가 호출하는 외부 메서드 수 | 10 이하 권장 |

> CC는 소스 텍스트 기반 키워드 카운팅으로 계산합니다. AST 노드 기반보다 빠르지만 약간 근사치입니다.

---

## STEP 5 — GraphModelMapper (mapper/graph_model_mapper.py)

**역할**: `ClassInfo[]`를 Neo4j에 넣을 수 있는 노드·관계 딕셔너리 목록으로 변환합니다.

**생성되는 노드 종류**:

| 레이블 | 고유키 | 예시 |
|--------|--------|------|
| `:Project` | `id` | `mes4u` |
| `:JavaFile` | `id` (파일경로) | `src/main/java/...` |
| `:Class` | `fqn` | `com.example.OrderService` |
| `:Interface` | `fqn` | `com.example.OrderRepository` |
| `:Method` | `id` | `com.example.OrderService#createOrder` |
| `:Field` | `id` | `com.example.OrderService.orderRepo` |
| `:Annotation` | `id` | `annotation:Transactional` |

**생성되는 관계 종류**:

```
(:JavaFile)   -[:DECLARES]->      (:Class / :Interface)
(:Class)      -[:HAS_METHOD]->    (:Method)
(:Class)      -[:HAS_FIELD]->     (:Field)
(:Class)      -[:EXTENDS]->       (:Class)
(:Class)      -[:IMPLEMENTS]->    (:Interface)
(:Class)      -[:DEPENDS_ON]->    (:Class / :Interface)   ← 필드 타입이 내부 클래스일 때
(:Method)     -[:CALLS]->         (:Method)
(:Method)     -[:ANNOTATED_WITH]->(:Annotation)
```

**2차 패스 방식**:
1. **1차**: 전체 클래스를 `class_registry`에 등록 (`단순명 → ClassInfo`, `FQN → ClassInfo`)
2. **2차**: 노드·관계 생성 시 `class_registry`를 참조하여 `extends/implements/DEPENDS_ON` 대상 해석

> `class_registry`에 없는 클래스(외부 라이브러리 등)는 관계를 생성하지 않습니다.

---

## STEP 6 — Neo4jLoader (loader/neo4j_loader.py)

**역할**: GraphData의 노드·관계를 Neo4j에 `UNWIND + MERGE` 배치 방식으로 적재합니다.

**적재 순서** (참조 무결성을 위해 순서가 중요):
```
1. :Project
2. :JavaFile
3. :Class / :Interface / :Enum
4. :Method / :Field / :Annotation
5. 모든 관계 (노드가 모두 존재한 뒤에 연결)
```

**배치 MERGE 방식**:
```cypher
-- 500건씩 묶어서 한 번에 처리
UNWIND $batch AS n
MERGE (node:Class {fqn: n.fqn})
SET node += n
```
- `MERGE`: 이미 있으면 업데이트, 없으면 생성 → 중복 방지 & 증분 갱신
- `batch_size=500` (기본값): 네트워크 왕복 횟수 최소화

**관계 적재 방식** (레이블 기반 MATCH):
```cypher
-- from_label / to_label을 사용해 인덱스를 타는 MATCH
UNWIND $batch AS e
MATCH (a:Class {fqn: e.from_id})
MATCH (b:Method {id: e.to_id})
MERGE (a)-[r:HAS_METHOD]->(b)
SET r += e.props
```
레이블 없이 전체 노드를 스캔하면 인덱스를 활용하지 못해 누락이 발생할 수 있으므로, 엣지에 `from_label`/`to_label`을 명시하여 레이블 기반 MATCH를 사용합니다.

**초기화 (최초 1회)**:
```python
loader.create_constraints_and_indexes()
```
`:Class(fqn)`, `:Method(id)` 등 유니크 제약 조건과 검색 인덱스를 생성합니다.

---

## 설정 파일 연결 구조

```
config/settings.py
├── CollectorConfig
│   ├── base_path      → STEP 1에서 탐색할 Java 소스 루트
│   ├── include_test   → 테스트 파일 포함 여부
│   └── file_encoding  → 파일 인코딩 (기본 utf-8)
│
├── Neo4jConfig
│   ├── uri / user / password / database
│   └── 환경변수 NEO4J_* 로 오버라이드 가능
│
└── IngestionConfig    ← 전체를 묶는 최상위 설정
    ├── project_id / project_name  → :Project 노드 생성
    ├── collector  → CollectorConfig
    ├── neo4j      → Neo4jConfig
    └── batch_size → STEP 6 배치 크기
```

CLI 인자로 각 설정을 오버라이드할 수 있습니다:
```powershell
python -m ingestion.main "C:\other\project\src\main\java" `
  --project-id myproject `
  --batch-size 200 `
  --log-level DEBUG
```
인자를 생략하면 `settings.py`의 기본값이 사용됩니다.

---

## 파일 구조 요약

```
ingestion/
├── main.py                      진입점, 6단계 파이프라인 실행
├── models/
│   └── code_info.py             ClassInfo / MethodInfo / CallInfo / GraphData
├── collector/
│   ├── source_collector.py      STEP 1: .java 파일 수집
│   └── incremental_tracker.py   STEP 2: 변경 파일만 선별
├── parser/
│   ├── ast_parser.py            STEP 3: AST 파싱 → ClassInfo
│   └── complexity_analyzer.py   STEP 4: 복잡도 메트릭 계산
├── mapper/
│   └── graph_model_mapper.py    STEP 5: ClassInfo → 노드/관계 변환
└── loader/
    └── neo4j_loader.py          STEP 6: Neo4j 배치 적재
```
