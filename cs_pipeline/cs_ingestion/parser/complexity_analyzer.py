"""
Module: CsComplexityAnalyzer (모듈 ③)
역할: 순환복잡도(CC), 인지복잡도(CogC), 코드라인 수(LOC) 등 메트릭을 계산하여
      ClassInfo 내 MethodInfo 객체에 추가한다.

Java 버전과의 차이:
    foreach 키워드 추가 (C# 전용 반복문)
    그 외 알고리즘은 동일 (텍스트 기반 근사 계산)
"""
import logging
import re

from cs_ingestion.models import ClassInfo, MethodInfo

logger = logging.getLogger(__name__)


class CsComplexityAnalyzer:
    """
    ClassInfo 내 각 MethodInfo에 복잡도 메트릭을 계산하여 채워 넣는 분석기.

    계산 메트릭:
        cyclomatic_complexity : McCabe 순환복잡도 (분기 경로 수 + 1)
        cognitive_complexity  : SonarQube 방식 인지복잡도 (중첩 깊이 가중)
        loc                   : 실행 코드 라인 수 (빈줄·주석 제외)
        param_count           : 파라미터 개수
        fan_out               : 이 메서드가 호출하는 외부 메서드 수
    """

    def enrich(self, class_info: ClassInfo) -> ClassInfo:
        """ClassInfo 내 모든 메서드에 복잡도 메트릭을 계산하여 채웁니다."""
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

    def _analyze_method(self, method: MethodInfo) -> None:
        snippet = method.source_snippet or ""
        method.loc = self._count_loc(snippet)
        method.param_count = len(method.params)
        method.fan_out = len(method.calls)
        method.cyclomatic_complexity = self._calc_cc(snippet)
        method.cognitive_complexity = self._calc_cognitive_complexity(snippet)

    # ── 순환복잡도 ───────────────────────────────────────────────

    def _calc_cc(self, source: str) -> int:
        """
        McCabe 순환복잡도를 텍스트 기반으로 계산합니다.

        분기 포인트: if, for, foreach, while, do, case, catch, &&, ||, ? (삼항)
        Java 버전 대비 foreach 키워드 추가.
        """
        if not source:
            return 1
        cc = 1
        keywords = {
            r"\bif\b": 1,
            r"\belse\s+if\b": 0,
            r"\bfor\b": 1,
            r"\bforeach\b": 1,
            r"\bwhile\b": 1,
            r"\bdo\b": 1,
            r"\bcase\b": 1,
            r"\bcatch\b": 1,
        }
        for pattern, weight in keywords.items():
            cc += len(re.findall(pattern, source)) * weight
        cc += source.count(" && ") + source.count(" || ")
        cc += source.count(" ? ")
        return max(1, cc)

    # ── 인지복잡도 ───────────────────────────────────────────────

    def _calc_cognitive_complexity(self, source: str) -> int:
        """SonarQube 방식의 인지복잡도를 계산합니다. foreach 키워드 추가."""
        cog = 0
        depth = 0
        nesting_open = re.compile(
            r"\b(if|else\s+if|for|foreach|while|do|try|catch|finally|switch)\b"
        )
        for line in source.splitlines():
            stripped = line.strip()
            if nesting_open.search(stripped):
                cog += 1 + depth
                if "{" in stripped:
                    depth += 1
            elif stripped.startswith("}"):
                depth = max(0, depth - 1)
            cog += stripped.count("&&") + stripped.count("||")
        return cog

    # ── LOC ──────────────────────────────────────────────────────

    @staticmethod
    def _count_loc(source: str) -> int:
        """빈 줄과 주석(//, /* */)을 제외한 실행 코드 라인 수를 반환합니다."""
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
