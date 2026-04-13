"""
Module: ASTParser (모듈 ②) ★ 핵심 모듈
역할: javalang으로 Java 소스를 AST로 파싱 — 클래스·메서드·필드·호출관계를 구조화된 객체로 변환.
Java 14+ 신문법(record, sealed) 파싱 실패 시 parse_failures.log에 기록.
"""
import logging
import re
from pathlib import Path
from typing import Optional

import javalang
import javalang.tree

from ingestion.models import ClassInfo, MethodInfo, CallInfo

logger = logging.getLogger(__name__)

# 파싱 실패 파일 목록 로그
_FAILURE_LOG = Path("parse_failures.log")


class ASTParser:
    """
    Java 소스코드를 파싱하여 ClassInfo 객체로 변환하는 파서 클래스.

    사용하는 라이브러리:
        javalang — 순수 Python으로 구현된 Java 파서 라이브러리.
        소스코드를 AST(Abstract Syntax Tree, 추상 구문 트리)라는 트리 구조로 분해합니다.
        AST를 사람이 보기 좋은 ClassInfo 객체로 변환하는 것이 이 클래스의 역할입니다.

    파싱 실패 처리:
        Java 14 이후 도입된 record, sealed class 등 신문법은 javalang이 지원하지 않아
        파싱에 실패할 수 있습니다. 이 경우 parse_failures.log에 파일 경로와 오류 메시지를
        기록하고 None을 반환합니다. 파이프라인 전체가 중단되지 않습니다.
    """

    def parse(self, file_path: str, source_code: str) -> Optional[ClassInfo]:
        """
        Java 소스코드 문자열 하나를 파싱하여 ClassInfo 객체를 반환합니다.
        파싱에 실패하면 None을 반환합니다.

        처리 흐름:
            1단계: javalang.parse.parse(source_code)로 전체 AST를 생성합니다.
                   실패하면 로그를 남기고 None 반환.
            2단계: AST에서 ClassDeclaration을 찾아 _build_class_info()를 호출합니다.
                   파일에 클래스가 여러 개 있어도 첫 번째 클래스만 처리합니다.
            3단계: ClassDeclaration이 없으면 InterfaceDeclaration을 찾습니다.
                   is_interface=True로 ClassInfo를 생성합니다.
            4단계: 클래스도 인터페이스도 없으면 None을 반환합니다.

        매개변수:
            file_path:   소스파일 경로 (로그·노드 ID에 사용)
            source_code: 파일 전체 소스코드 문자열

        반환값:
            ClassInfo 객체 (성공) | None (실패)
        """
        # ── 1단계: javalang으로 소스 파싱 ───────────────────────
        try:
            tree = javalang.parse.parse(source_code)
        except Exception as exc:
            logger.warning("파싱 실패: %s — %s", file_path, exc)
            self._log_failure(file_path, str(exc))
            return None

        source_lines = source_code.splitlines()
        package = tree.package.name if tree.package else ""

        # ── 2단계: ClassDeclaration 탐색 ────────────────────────
        for _, cls in tree.filter(javalang.tree.ClassDeclaration):
            return self._build_class_info(
                file_path, package, cls, tree, source_lines, is_interface=False
            )

        # 인터페이스도 ClassInfo로 파싱 (is_interface=True)
        for _, iface in tree.filter(javalang.tree.InterfaceDeclaration):
            return self._build_class_info(
                file_path, package, iface, tree, source_lines, is_interface=True
            )

        logger.debug("클래스/인터페이스 선언 없음: %s", file_path)
        return None

    # ── 내부 빌드 메서드 ─────────────────────────────────────────

    def _build_class_info(
        self,
        file_path: str,
        package: str,
        cls_node,
        tree,
        source_lines: list[str],
        is_interface: bool,
    ) -> ClassInfo:
        """
        javalang의 ClassDeclaration 또는 InterfaceDeclaration AST 노드를
        ClassInfo 객체로 변환합니다.

        처리 순서:
            1. extends 파싱:
               - ClassDeclaration: extends는 단일 객체 (부모 클래스는 하나뿐)
               - InterfaceDeclaration: extends는 리스트 (인터페이스는 여러 개 상속 가능)
               두 경우를 모두 처리합니다.
            2. ClassInfo 기본 정보 설정 (이름, 패키지, FQN, 어노테이션 등)
            3. 필드 수집: tree.filter(FieldDeclaration)으로 전체 필드를 추출합니다.
               { 변수명 → 타입명 } 딕셔너리로 저장합니다.
               이 정보는 나중에 메서드 호출 관계(CALLS)를 해석할 때 사용합니다.
            4. 메서드 수집: tree.filter(MethodDeclaration)으로 메서드를 추출합니다.
            5. 생성자 수집: ConstructorDeclaration도 메서드처럼 수집합니다.
               키 이름은 "<init>_{클래스명}" 형태를 사용합니다.

        매개변수:
            file_path:    소스파일 경로
            package:      패키지명
            cls_node:     javalang AST 노드 (ClassDeclaration 또는 InterfaceDeclaration)
            tree:         전체 파싱 트리 (필드·메서드 탐색에 사용)
            source_lines: 소스코드를 줄 단위로 분할한 리스트
            is_interface: 인터페이스 여부
        """
        modifiers = cls_node.modifiers or set()

        # InterfaceDeclaration.extends는 list, ClassDeclaration.extends는 단일 객체
        _ext = cls_node.extends if hasattr(cls_node, "extends") and cls_node.extends else None
        if isinstance(_ext, list):
            extends = _ext[0].name if _ext else None
        else:
            extends = _ext.name if _ext else None

        class_info = ClassInfo(
            file_path=file_path,
            package=package,
            class_name=cls_node.name,
            extends=extends,
            implements=[i.name for i in (cls_node.implements or [])] if hasattr(cls_node, "implements") else [],
            annotations=[a.name for a in (cls_node.annotations or [])],
            is_abstract="abstract" in modifiers,
            is_final="final" in modifiers,
            is_interface=is_interface,
            fqn=f"{package}.{cls_node.name}" if package else cls_node.name,
            line_start=cls_node.position.line if cls_node.position else 0,
        )

        # ── 3단계: 필드 수집 (의존성 해석 키 구성) ───────────────
        for _, fd in tree.filter(javalang.tree.FieldDeclaration):
            type_name = self._get_type_name(fd.type)
            for declarator in fd.declarators:
                class_info.fields[declarator.name] = type_name

        # ── 4단계: 메서드 수집 ────────────────────────────────────
        for _, method in tree.filter(javalang.tree.MethodDeclaration):
            method_info = self._build_method_info(
                method, class_info, source_lines
            )
            class_info.methods[method.name] = method_info

        # 생성자도 메서드처럼 수집
        for _, ctor in tree.filter(javalang.tree.ConstructorDeclaration):
            method_info = self._build_constructor_info(ctor, class_info, source_lines)
            class_info.methods[f"<init>_{ctor.name}"] = method_info

        return class_info

    def _build_method_info(
        self,
        method,
        class_info: ClassInfo,
        source_lines: list[str],
    ) -> MethodInfo:
        """
        javalang의 MethodDeclaration AST 노드를 MethodInfo 객체로 변환합니다.

        처리 내용:
            - 시작 라인(method.position.line)을 기준으로 중괄호 깊이를 추적하여
              메서드 종료 라인을 추정합니다 (_find_end_line 사용).
            - 시작~종료 라인 범위의 소스코드를 source_snippet으로 저장합니다.
              이 원문은 ComplexityAnalyzer에서 복잡도를 계산하는 데 사용됩니다.
            - 파라미터 타입 목록과 반환 타입을 추출하여 signature를 구성합니다.
              예: "createOrder(Order,String):boolean"
            - id는 "{FQN}#{메서드명}" 형태로 Neo4j Method 노드의 고유 식별자가 됩니다.
            - _extract_calls()를 호출하여 이 메서드 내부의 메서드 호출 목록을 추출합니다.

        매개변수:
            method:       javalang MethodDeclaration AST 노드
            class_info:   이 메서드가 속한 클래스 정보 (FQN, fields에 사용)
            source_lines: 전체 소스코드 줄 목록
        """
        start = method.position.line if method.position else 0
        end = self._find_end_line(source_lines, start)
        snippet = self._snippet(source_lines, start, end)

        params = [self._get_type_name(p.type) for p in (method.parameters or [])]
        return_type = self._get_type_name(method.return_type)
        signature = f"{method.name}({','.join(params)}):{return_type}"

        fqn_id = f"{class_info.fqn}#{method.name}"

        m = MethodInfo(
            name=method.name,
            return_type=return_type,
            params=params,
            visibility=self._get_visibility(method.modifiers),
            is_static="static" in (method.modifiers or set()),
            annotations=[a.name for a in (method.annotations or [])],
            start_line=start,
            end_line=end,
            source_snippet=snippet,
            id=fqn_id,
            signature=signature,
        )

        # ── 5단계: 호출관계 추출 (필드 타입 맵 사용) ─────────────
        m.calls = self._extract_calls(method, class_info.fields)
        return m

    def _build_constructor_info(
        self,
        ctor,
        class_info: ClassInfo,
        source_lines: list[str],
    ) -> MethodInfo:
        """
        javalang의 ConstructorDeclaration AST 노드를 MethodInfo 객체로 변환합니다.

        생성자는 반환 타입이 없으므로 return_type을 "void"로 고정합니다.
        id는 "{FQN}#<init>" 형태를 사용합니다.
        그 외 처리 방식은 _build_method_info()와 동일합니다.

        매개변수:
            ctor:         javalang ConstructorDeclaration AST 노드
            class_info:   이 생성자가 속한 클래스 정보
            source_lines: 전체 소스코드 줄 목록
        """
        start = ctor.position.line if ctor.position else 0
        end = self._find_end_line(source_lines, start)
        snippet = self._snippet(source_lines, start, end)

        params = [self._get_type_name(p.type) for p in (ctor.parameters or [])]
        signature = f"{ctor.name}({','.join(params)}):void"
        fqn_id = f"{class_info.fqn}#<init>"

        m = MethodInfo(
            name=ctor.name,
            return_type="void",
            params=params,
            visibility=self._get_visibility(ctor.modifiers),
            is_static=False,
            annotations=[a.name for a in (ctor.annotations or [])],
            start_line=start,
            end_line=end,
            source_snippet=snippet,
            id=fqn_id,
            signature=signature,
        )
        m.calls = self._extract_calls(ctor, class_info.fields)
        return m

    # ── 호출 관계 추출 ───────────────────────────────────────────

    def _extract_calls(self, method_node, field_map: dict[str, str]) -> list[CallInfo]:
        """
        메서드 AST 노드에서 메서드 호출(MethodInvocation) 목록을 추출하여
        CallInfo 리스트로 반환합니다.

        동작 원리:
            Java 코드에서 "objectName.methodName()" 형태의 호출이 있을 때:
                qualifier = "objectName"   (점 앞의 변수명)
                member    = "methodName"   (실제 호출하는 메서드명)

            qualifier가 필드명이면 field_map을 통해 실제 클래스명으로 해석합니다.
            예: field_map = { "orderRepo": "OrderRepository" }
                qualifier = "orderRepo" → resolved_class = "OrderRepository"
                → CallInfo(callee_class="OrderRepository", callee_method="save")

        제외 조건:
            - qualifier가 없는 경우 (같은 클래스 내 메서드 호출, this 생략)
            - qualifier가 "this" 또는 "super"인 경우 (자기 자신 호출)
            - 동일한 {클래스#메서드} 조합이 중복된 경우 (한 메서드 내 중복 호출)

        매개변수:
            method_node: javalang 메서드/생성자 AST 노드
            field_map:   { 변수명 → 타입명 } 딕셔너리 (클래스의 필드 선언 목록)

        반환값:
            CallInfo 객체 리스트 (중복 제거됨)
        """
        calls: list[CallInfo] = []
        seen: set[str] = set()

        for _, invocation in method_node.filter(javalang.tree.MethodInvocation):
            qualifier = invocation.qualifier or ""
            member = invocation.member

            if not qualifier:
                continue

            # qualifier가 필드명이면 실제 클래스명으로 해석
            resolved_class = field_map.get(qualifier, qualifier)

            # 자기 자신 호출(this) 제외
            if resolved_class.lower() in ("this", "super"):
                continue

            key = f"{resolved_class}#{member}"
            if key in seen:
                continue
            seen.add(key)

            line = invocation.position.line if invocation.position else 0
            calls.append(
                CallInfo(
                    callee_class=resolved_class,
                    callee_method=member,
                    call_type="method",
                    line=line,
                )
            )

        return calls

    # ── 유틸리티 ─────────────────────────────────────────────────

    def _find_end_line(self, lines: list[str], start: int) -> int:
        """
        소스코드에서 메서드의 종료 라인 번호를 추정합니다.

        알고리즘:
            start 라인부터 한 줄씩 읽으면서 '{' 와 '}' 개수를 누적합니다.
            처음 '{' 가 등장한 후 누적값이 0 이하가 되는 라인이 종료 라인입니다.

        예시:
            10: public void foo() {    → depth: 1, started=True
            11:     if (x) {           → depth: 2
            12:         doSomething(); → depth: 2
            13:     }                  → depth: 1
            14: }                      → depth: 0  ← 종료 라인 = 14 반환

        start가 0 이하이거나 중괄호를 찾지 못하면 start+50을 폴백으로 반환합니다.

        매개변수:
            lines: 전체 소스코드 줄 목록 (0-indexed)
            start: 메서드 시작 라인 번호 (1-indexed, javalang 기준)
        """
        if start <= 0:
            return start + 50

        depth = 0
        started = False
        for i, line in enumerate(lines[start - 1:], start=start):
            depth += line.count("{") - line.count("}")
            if "{" in line:
                started = True
            if started and depth <= 0:
                return i

        return start + 50  # 파싱 실패 시 fallback

    @staticmethod
    def _snippet(lines: list[str], start: int, end: int) -> str:
        """
        소스코드 줄 목록에서 start~end 범위의 코드를 잘라 문자열로 반환합니다.

        start, end는 1-indexed(javalang 기준) 라인 번호입니다.
        내부에서 0-indexed로 변환하여 슬라이싱합니다.
        start가 0 이하이면 빈 문자열을 반환합니다.

        매개변수:
            lines: 전체 소스코드 줄 목록
            start: 시작 라인 번호 (1-indexed)
            end:   종료 라인 번호 (1-indexed, 포함)
        """
        if start <= 0:
            return ""
        s = max(0, start - 1)
        e = min(len(lines), end)
        return "\n".join(lines[s:e])

    @staticmethod
    def _get_type_name(type_node) -> str:
        """
        javalang의 타입 AST 노드에서 단순 타입명 문자열을 추출합니다.

        처리 케이스:
            - None: "void" 반환 (반환 타입이 없는 경우)
            - ReferenceType (예: Order, String): .name 속성에서 추출
            - BasicType (예: int, boolean): .name 속성에서 추출
            - 중첩 타입(예: Map.Entry): sub_type.name에서 추출
            - 기타: str() 변환으로 폴백

        예시:
            javalang이 파싱한 "List<Order>" → "List" (제네릭 타입 인수는 무시)
            javalang이 파싱한 "void" → "void"
        """
        if type_node is None:
            return "void"
        if hasattr(type_node, "name"):
            return type_node.name
        if hasattr(type_node, "sub_type") and type_node.sub_type:
            return type_node.sub_type.name
        return str(type_node)

    @staticmethod
    def _get_visibility(modifiers) -> str:
        """
        modifiers 집합에서 Java 접근제어자를 추출하여 문자열로 반환합니다.

        modifiers는 {"public", "static", "final"} 같은 문자열 집합입니다.
        public > protected > private 순서로 확인하여 첫 번째로 발견된 값을 반환합니다.
        접근제어자가 없으면 "package" (package-private)를 반환합니다.
        modifiers 자체가 None이거나 비어 있으면 "package"를 반환합니다.
        """
        if not modifiers:
            return "package"
        for mod in ("public", "protected", "private"):
            if mod in modifiers:
                return mod
        return "package"

    @staticmethod
    def _log_failure(file_path: str, reason: str) -> None:
        """
        파싱에 실패한 파일 정보를 parse_failures.log에 추가 기록합니다.

        파일이 없으면 새로 만들고, 있으면 끝에 이어 씁니다 (append 모드).
        기록 형식: "{파일경로}\\t{오류 메시지}\\n"
        로그 쓰기 자체가 실패해도 예외를 무시합니다 (파이프라인 중단 방지).
        """
        try:
            with open(_FAILURE_LOG, "a", encoding="utf-8") as f:
                f.write(f"{file_path}\t{reason}\n")
        except Exception:
            pass
