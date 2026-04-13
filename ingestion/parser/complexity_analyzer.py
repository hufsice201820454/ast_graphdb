"""
Module: ComplexityAnalyzer (모듈 ③)
역할: 순환복잡도(CC), 인지복잡도(CogC), 코드라인 수(LOC) 등 메트릭을 계산하여
      ClassInfo 내 MethodInfo 객체에 추가한다.

메트릭 기준값 (설계문서 3.4.1):
    CC   : 1~4 단순, 5~10 보통, 11~20 복잡, 21+ 매우 복잡
    CogC : 15 이하 권장 (SonarQube 방식)
    LOC  : 메서드 20 이하 권장, 50 초과 시 분리 고려
    Params: 4 이하 권장
    Fanout: 10 이하 권장
"""
import logging

import javalang
import javalang.tree

from ingestion.models import ClassInfo, MethodInfo

logger = logging.getLogger(__name__)

# 순환복잡도 판정 노드 타입
_CC_DECISION_NODES = (
    javalang.tree.IfStatement,
    javalang.tree.ForStatement,
    javalang.tree.WhileStatement,
    javalang.tree.DoStatement,
    javalang.tree.SwitchStatementCase,
    javalang.tree.CatchClause,
    javalang.tree.TernaryExpression,
)

# 인지복잡도 중첩 구조 증가 노드
_NESTING_NODES = (
    javalang.tree.IfStatement,
    javalang.tree.ForStatement,
    javalang.tree.WhileStatement,
    javalang.tree.DoStatement,
    javalang.tree.TryStatement,
)


class ComplexityAnalyzer:
    """
    ClassInfo 내 각 MethodInfo에 복잡도 메트릭을 계산하여 채워 넣는 분석기.

    ASTParser가 만든 ClassInfo를 받아 각 메서드의 source_snippet(소스코드 원문)을
    분석하여 아래 메트릭을 계산하고 MethodInfo 필드에 저장합니다.

    계산하는 메트릭:
        cyclomatic_complexity (CC):
            McCabe의 순환복잡도. 분기 경로(if/for/while/case/&&/|| 등)의 수 + 1.
            높을수록 테스트하기 어렵고 버그가 숨기 쉬운 메서드입니다.

        cognitive_complexity (CogC):
            SonarQube 방식의 인지복잡도. 중첩 깊이에 가중치를 부여합니다.
            if 안에 for 안에 if가 있으면 CC보다 훨씬 높게 계산됩니다.
            사람이 코드를 이해하기 얼마나 어려운지를 수치화합니다.

        loc:
            실행 코드 라인 수. 빈 줄, 주석(//, /* */) 제외.

        param_count:
            파라미터 개수. 이미 ASTParser가 수집한 params 리스트의 길이입니다.

        fan_out:
            이 메서드가 호출하는 외부 메서드 수 (calls 리스트의 길이).
    """

    def enrich(self, class_info: ClassInfo) -> ClassInfo:
        """
        ClassInfo 내 모든 메서드에 복잡도 메트릭을 계산하여 채웁니다.

        class_info.methods 딕셔너리를 순회하며 각 MethodInfo에 대해
        _analyze_method()를 호출합니다.

        개별 메서드 분석 중 예외가 발생해도 해당 메서드만 건너뛰고 계속 진행합니다.
        (소스 파싱 이상 등으로 일부 메서드에 source_snippet이 없을 수 있음)

        매개변수:
            class_info: ASTParser가 생성한 ClassInfo 객체

        반환값:
            복잡도가 채워진 같은 ClassInfo 객체 (수정 후 반환)
        """
        for method_name, method in class_info.methods.items():
            try:
                self._analyze_method(method)
            except Exception as exc:
                logger.debug(
                    "복잡도 계산 실패: %s#%s — %s",
                    class_info.class_name,
                    method_name,
                    exc,
                )
        return class_info

    # ── 메서드 단위 분석 ─────────────────────────────────────────

    def _analyze_method(self, method: MethodInfo) -> None:
        """
        MethodInfo 하나에 대해 모든 복잡도 메트릭을 계산하고 필드에 저장합니다.

        source_snippet(메서드 소스코드 원문)을 입력으로 사용합니다.
        snippet이 비어 있으면 각 계산 함수가 기본값을 반환합니다.

        저장하는 메트릭:
            method.loc                   ← _count_loc()
            method.param_count           ← len(method.params) (이미 ASTParser가 수집)
            method.fan_out               ← len(method.calls)  (이미 ASTParser가 수집)
            method.cyclomatic_complexity ← _calc_cc_from_source()
            method.cognitive_complexity  ← _calc_cognitive_complexity()
        """
        snippet = method.source_snippet or ""

        method.loc = self._count_loc(snippet)
        method.param_count = len(method.params)
        method.fan_out = len(method.calls)
        method.cyclomatic_complexity = self._calc_cc_from_source(snippet)
        method.cognitive_complexity = self._calc_cognitive_complexity(snippet)

    # ── 순환복잡도 (McCabe CC) ────────────────────────────────────

    def _calc_cc_from_source(self, source: str) -> int:
        """
        소스코드 텍스트를 기반으로 McCabe 순환복잡도를 계산합니다.

        계산 방식:
            기본값 1에서 시작하여 아래 분기 포인트마다 1씩 더합니다:
                if        → +1 (else if도 if로 카운트됨)
                for       → +1
                while     → +1
                do        → +1
                case      → +1 (switch의 각 case)
                catch     → +1
                &&, ||    → +1 (논리 연산자, 단락 평가로 인한 분기)
                ? (삼항)   → +1

            AST 기반이 아닌 정규식 텍스트 기반이므로 근사치입니다.
            문자열 리터럴 안의 키워드도 카운트될 수 있습니다.

        매개변수:
            source: 메서드 소스코드 원문 문자열

        반환값:
            순환복잡도 정수값 (최소 1)
        """
        if not source:
            return 1

        cc = 1  # 기본값: 선형 경로

        # 키워드 기반 카운팅 (정규식으로 독립 단어 판별)
        keywords = {
            r"\bif\b": 1,
            r"\belse\s+if\b": 0,  # else if는 if로 이미 카운트
            r"\bfor\b": 1,
            r"\bwhile\b": 1,
            r"\bdo\b": 1,
            r"\bcase\b": 1,
            r"\bcatch\b": 1,
        }
        import re
        for pattern, weight in keywords.items():
            cc += len(re.findall(pattern, source)) * weight

        # 논리 연산자 (&&, ||)
        cc += source.count(" && ") + source.count(" || ")
        # 삼항 연산자
        cc += source.count(" ? ")

        return max(1, cc)

    # ── 인지복잡도 (SonarQube 방식) ──────────────────────────────

    def _calc_cognitive_complexity(self, source: str) -> int:
        """
        SonarQube 방식의 인지복잡도를 계산합니다.

        순환복잡도(CC)와의 차이:
            CC는 모든 분기에 동일하게 +1을 더합니다.
            인지복잡도는 중첩 깊이에 따라 가중치가 증가합니다.

            예시 (CC=3 이지만 CogC는 다름):
                if (a) {             → +1 (depth=0이므로 +1+0=1)
                    for (x) {        → +2 (depth=1이므로 +1+1=2)
                        if (b) { }   → +3 (depth=2이므로 +1+2=3)
                    }
                }
                → CogC = 6

        계산 방식 (라인 기반 근사):
            - if/else if/for/while/do/try/catch/finally/switch 키워드가 있는 라인: +1 + 현재 중첩 깊이
            - { 가 있으면 다음 라인부터 중첩 깊이 +1
            - } 로 시작하는 라인이 있으면 중첩 깊이 -1
            - &&, || 논리 연산자: 중첩 깊이 무관하게 각 +1

        매개변수:
            source: 메서드 소스코드 원문 문자열

        반환값:
            인지복잡도 정수값
        """
        import re

        cog = 0
        depth = 0

        # 라인 단위로 간단히 추정
        nesting_open = re.compile(
            r"\b(if|else\s+if|for|while|do|try|catch|finally|switch)\b"
        )
        for line in source.splitlines():
            stripped = line.strip()
            if nesting_open.search(stripped):
                cog += 1 + depth
                if "{" in stripped:
                    depth += 1
            elif stripped.startswith("}"):
                depth = max(0, depth - 1)
            # 논리 연산자 (중첩 깊이 무관)
            cog += stripped.count("&&") + stripped.count("||")

        return cog

    # ── LOC (코드 라인 수) ───────────────────────────────────────

    @staticmethod
    def _count_loc(source: str) -> int:
        """
        소스코드에서 실행 코드 라인 수(LOC, Lines of Code)를 계산합니다.

        제외 대상:
            - 빈 줄 (공백만 있는 줄 포함)
            - 단일 라인 주석으로 시작하는 줄 (//)
            - 블록 주석 내부 줄 (/* ... */)

        블록 주석 처리:
            /* 가 등장하면 in_block_comment=True로 전환합니다.
            같은 줄에 */ 가 있으면 한 줄 블록 주석으로 처리합니다.
            */ 가 나올 때까지의 줄은 모두 LOC에서 제외합니다.

        매개변수:
            source: 메서드 소스코드 원문 문자열

        반환값:
            실행 코드 라인 수 정수값
        """
        if not source:
            return 0

        loc = 0
        in_block_comment = False
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if in_block_comment:
                if "*/" in stripped:
                    in_block_comment = False
                continue
            if stripped.startswith("/*"):
                in_block_comment = True
                if "*/" not in stripped:
                    continue
            if stripped.startswith("//"):
                continue
            loc += 1
        return loc
