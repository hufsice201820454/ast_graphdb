from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CallInfo:
    """
    메서드가 다른 메서드를 호출하는 관계를 나타내는 데이터 클래스.

    예를 들어 OrderService.CreateOrder() 안에서
    orderRepo.Save(order) 를 호출한다면:
        callee_class  = "OrderRepository"
        callee_method = "Save"
        line          = 실제 호출한 소스코드 라인 번호

    이 정보는 Neo4j에서 (:Method)-[:CALLS]->(:Method) 관계를 만드는 데 사용됩니다.
    """
    callee_class: str        # 호출 대상 클래스명
    callee_method: str       # 호출 대상 메서드명
    call_type: str = "method"  # method | static | constructor
    line: int = 0


@dataclass
class MethodInfo:
    """
    C# 메서드 하나를 파이썬 객체로 표현한 데이터 클래스.

    id 예시:    "PNT.Common.OrderService#CreateOrder"
    signature 예시: "CreateOrder(Order,string):bool"

    도메인 특화 필드:
        action_kind  — new SmartPair(CommonData.ACTION_KIND, "GetxxxxInfo") 에서
                       ACTION_KIND 키를 가진 SmartPair의 두 번째 파라미터.
                       이 메서드가 백엔드에서 호출하는 액션 메서드명.
        call_backend — FxResponseDataTable(PNT.COMMON.COMMONDATA.CxxxY, ...) 에서
                       첫 번째 파라미터. 백엔드 호출 경로(FQN 형태).
    """
    name: str                                           # 메서드 이름
    return_type: str                                    # 반환 타입
    params: list[str] = field(default_factory=list)     # 파라미터 타입 목록
    visibility: str = "private"                         # public | protected | private | internal
    is_static: bool = False                             # static 메서드 여부
    attributes: list[str] = field(default_factory=list) # C# 어트리뷰트 목록 ex) ["Obsolete"]
    start_line: int = 0
    end_line: int = 0
    source_snippet: str = ""                            # 메서드 전체 소스코드 원문
    calls: list[CallInfo] = field(default_factory=list) # 이 메서드 안에서 호출하는 메서드 목록

    # 도메인 특화 필드
    action_kind: str = ""   # SmartPair(ACTION_KIND, "xxx") 의 두 번째 파라미터
    call_backend: str = ""  # FxResponseDataTable(PNT.xxx.yyy, ...) 의 첫 번째 파라미터

    # 파생 속성 — CsParser 또는 GraphModelMapper가 생성
    id: str = ""            # Neo4j Method 노드 고유 식별자 ex) "PNT.Common.OrderService#CreateOrder"
    signature: str = ""     # ex) "CreateOrder(Order,string):bool"


@dataclass
class ClassInfo:
    """
    C# 클래스(또는 인터페이스/구조체) 하나를 파이썬 객체로 표현한 데이터 클래스.

    Java 버전과의 주요 차이:
        package   → namespace
        extends   → base_class  (C#은 단일 상속)
        is_final  → is_sealed
        annotation → attribute
        is_struct  필드 추가 (struct 타입 지원)

    한 .cs 파일에 클래스가 여러 개 있을 수 있으므로
    파서는 ClassInfo 목록(list[ClassInfo])을 반환합니다.

    fqn 예시: "PNT.Common.OrderService"
    fields 예시: { "orderRepo": "OrderRepository", "clock": "Clock" }
    methods 예시: { "CreateOrder": MethodInfo(...) }
    """
    file_path: str                                           # .cs 파일 경로
    namespace: str                                           # 네임스페이스 ex) PNT.Common
    class_name: str                                          # 클래스명 ex) OrderService
    base_class: Optional[str] = None                         # 부모 클래스명 (없으면 None)
    interfaces: list[str] = field(default_factory=list)      # 구현 인터페이스 목록
    attributes: list[str] = field(default_factory=list)      # 클래스 레벨 어트리뷰트 목록
    is_abstract: bool = False
    is_sealed: bool = False                                  # Java의 is_final에 대응
    is_interface: bool = False
    is_struct: bool = False
    fields: dict[str, str] = field(default_factory=dict)     # { 변수명 → 타입명 }
    methods: dict[str, "MethodInfo"] = field(default_factory=dict)  # { 메서드명 → MethodInfo }

    # 파생 속성 — CsParser 또는 GraphModelMapper가 채워 넣음
    fqn: str = ""           # ex) "PNT.Common.OrderService"
    line_start: int = 0
    line_end: int = 0


@dataclass
class GraphData:
    """
    Neo4j에 적재하기 직전 형태로 변환된 그래프 데이터.

    nodes 예시:
        { "label": "Class", "fqn": "PNT.Common.OrderService", "name": "OrderService", ... }
    edges 예시:
        { "type": "CALLS_BACKEND", "from_id": "PNT.Common.OrderService#CreateOrder",
          "to_id": "PNT.COMMON.COMMONDATA.CxxxY", "action_kind": "GetxxxxInfo" }
    """
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
