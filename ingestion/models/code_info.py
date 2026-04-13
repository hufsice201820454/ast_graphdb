from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CallInfo:
    """
    메서드가 다른 메서드를 호출하는 관계를 나타내는 데이터 클래스.

    예를 들어 OrderService.createOrder() 안에서
    orderRepo.save(order) 를 호출한다면:
        callee_class  = "OrderRepository"   (orderRepo 변수의 타입)
        callee_method = "save"
        line          = 실제 호출한 소스코드 라인 번호

    이 정보는 Neo4j에서 (:Method)-[:CALLS]->(:Method) 관계를 만드는 데 사용됩니다.
    """
    callee_class: str        # 호출 대상 클래스명 (단순명)
    callee_method: str       # 호출 대상 메서드명
    call_type: str = "method"  # method | static | super
    line: int = 0


@dataclass
class MethodInfo:
    """
    Java 메서드 하나를 파이썬 객체로 표현한 데이터 클래스.

    ASTParser가 메서드를 파싱하면 이 객체를 만들어 ClassInfo.methods 딕셔너리에 저장합니다.
    ComplexityAnalyzer가 나중에 복잡도 메트릭(cc, loc 등)을 채워 넣습니다.

    id 필드 예시: "com.example.order.OrderService#createOrder"
    signature 예시: "createOrder(Order,String):boolean"
    """
    name: str                                          # 메서드 이름 (예: createOrder)
    return_type: str                                   # 반환 타입 (예: void, boolean, Order)
    params: list[str] = field(default_factory=list)    # 파라미터 타입 목록 (예: ["Order", "String"])
    visibility: str = "package"                        # 접근제어자: public | protected | private | package
    is_static: bool = False                            # static 메서드 여부
    annotations: list[str] = field(default_factory=list)  # 어노테이션 목록 (예: ["Transactional", "Override"])
    start_line: int = 0                                # 소스코드에서 메서드 시작 라인
    end_line: int = 0                                  # 소스코드에서 메서드 종료 라인
    source_snippet: str = ""                           # 메서드 전체 소스코드 원문
    calls: list[CallInfo] = field(default_factory=list)  # 이 메서드 안에서 호출하는 메서드 목록

    # 복잡도 메트릭 — ComplexityAnalyzer가 계산 후 채워 넣음
    cyclomatic_complexity: int = 1   # 순환복잡도: 분기 경로 수 (if/for/while/case 마다 +1)
    cognitive_complexity: int = 0    # 인지복잡도: 중첩 깊이를 반영한 이해 난이도
    loc: int = 0                     # 실행 코드 라인 수 (빈줄·주석 제외)
    param_count: int = 0             # 파라미터 개수
    fan_out: int = 0                 # 이 메서드가 호출하는 외부 메서드 수

    # 파생 속성 — GraphModelMapper 또는 ASTParser가 생성
    id: str = ""         # Neo4j Method 노드의 고유 식별자 (FQN)
    signature: str = ""  # 메서드 시그니처 문자열


@dataclass
class ClassInfo:
    """
    Java 클래스(또는 인터페이스) 하나를 파이썬 객체로 표현한 데이터 클래스.

    ASTParser가 .java 파일을 파싱하면 이 객체 하나를 반환합니다.
    파이프라인 전체에서 중심 데이터 구조로 사용됩니다.

    fqn(Fully Qualified Name) 예시: "com.example.order.OrderService"
    fields 예시: { "orderRepo": "OrderRepository", "clock": "Clock" }
    methods 예시: { "createOrder": MethodInfo(...), "findById": MethodInfo(...) }
    """
    file_path: str                                          # .java 파일의 절대/상대 경로
    package: str                                            # 패키지명 (예: com.example.order)
    class_name: str                                         # 클래스명 (예: OrderService)
    extends: Optional[str] = None                          # 부모 클래스명 (없으면 None)
    implements: list[str] = field(default_factory=list)    # 구현하는 인터페이스 이름 목록
    annotations: list[str] = field(default_factory=list)   # 클래스 레벨 어노테이션 목록
    is_abstract: bool = False                               # abstract 클래스 여부
    is_final: bool = False                                  # final 클래스 여부
    is_interface: bool = False                              # interface 여부 (True면 Class 대신 Interface 레이블)
    fields: dict[str, str] = field(default_factory=dict)   # { 변수명 → 타입명 } 필드 선언 목록
    methods: dict[str, "MethodInfo"] = field(default_factory=dict)  # { 메서드명 → MethodInfo }
    external_clients: dict = field(default_factory=dict)   # FeignClient 등 외부 호출 클라이언트 정보

    # 파생 속성 — ASTParser 또는 GraphModelMapper가 채워 넣음
    fqn: str = ""        # 완전한 클래스 이름 (com.example.order.OrderService)
    line_start: int = 0  # 클래스 선언 시작 라인
    line_end: int = 0    # 클래스 선언 종료 라인


@dataclass
class GraphData:
    """
    Neo4j에 적재하기 직전 형태로 변환된 그래프 데이터.

    GraphModelMapper가 ClassInfo 목록을 변환하여 이 객체를 만들고,
    Neo4jLoader가 이 객체를 받아 Neo4j에 MERGE합니다.

    nodes 예시:
        { "label": "Class", "fqn": "com.example.OrderService", "name": "OrderService", ... }
    edges 예시:
        { "type": "HAS_METHOD", "from_id": "com.example.OrderService",
          "to_id": "com.example.OrderService#createOrder",
          "from_label": "Class", "to_label": "Method" }
    """
    nodes: list[dict] = field(default_factory=list)  # Neo4j에 MERGE할 노드 딕셔너리 목록
    edges: list[dict] = field(default_factory=list)  # Neo4j에 MERGE할 관계 딕셔너리 목록
