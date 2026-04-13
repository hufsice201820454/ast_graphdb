"""
Module: AnnotationResolver (3.3.7 Spring 프레임워크 어노테이션 특별 처리)
역할: @FeignClient 인터페이스를 외부 클라이언트로 등록하여
      의존관계 탐지 정확도를 높인다.

처리 대상:
    @Autowired  → field_map에 이미 포함되어 자동 처리됨
    @FeignClient → 구현 클래스처럼 외부 클라이언트로 등록
    @Value      → 외부 설정 주입이므로 의존성 그래프에서 제외
"""
import logging

import javalang
import javalang.tree

from ingestion.models import ClassInfo

logger = logging.getLogger(__name__)

# Spring 컴포넌트 어노테이션
_SPRING_COMPONENT_ANNOTATIONS = frozenset({
    "Component", "Service", "Repository", "Controller",
    "RestController", "Configuration", "Bean",
})

# 의존성 그래프에서 제외할 어노테이션이 붙은 필드
_EXCLUDED_FIELD_ANNOTATIONS = frozenset({"Value", "Autowired"})


class AnnotationResolver:
    """Spring 프레임워크 어노테이션 특별 처리기."""

    def resolve_spring_dependencies(
        self, tree, class_info: ClassInfo
    ) -> ClassInfo:
        """
        Spring 특수 어노테이션을 분석하여 ClassInfo를 보강한다.
        - @FeignClient 인터페이스: 외부 서비스 호출 클래스로 등록
        - @Value 필드: field_map에서 제거 (설정값 주입은 의존성 아님)
        """
        self._handle_feign_clients(tree, class_info)
        self._remove_value_fields(tree, class_info)
        return class_info

    # ── FeignClient 처리 ─────────────────────────────────────────

    def _handle_feign_clients(self, tree, class_info: ClassInfo) -> None:
        """
        @FeignClient 어노테이션이 붙은 인터페이스를
        외부 서비스 호출 클래스로 class_info.external_clients에 등록한다.
        """
        for _, iface in tree.filter(javalang.tree.InterfaceDeclaration):
            for ann in (iface.annotations or []):
                if ann.name == "FeignClient":
                    service_name = self._extract_feign_service_name(ann)
                    pseudo = {
                        "name": iface.name,
                        "type": "FeignClient",
                        "service": service_name,
                        "methods": [m.name for m in (iface.methods or [])],
                    }
                    class_info.external_clients[iface.name] = pseudo
                    logger.debug(
                        "FeignClient 등록: %s → %s", iface.name, service_name
                    )

    @staticmethod
    def _extract_feign_service_name(annotation) -> str:
        """@FeignClient(name="...")에서 서비스명을 추출한다."""
        if not annotation.element:
            return "unknown"
        # name= 또는 value= 속성
        if isinstance(annotation.element, list):
            for elem in annotation.element:
                if hasattr(elem, "name") and elem.name in ("name", "value"):
                    return str(elem.value) if elem.value else "unknown"
        if hasattr(annotation.element, "value"):
            return str(annotation.element.value)
        return "unknown"

    # ── @Value 필드 제거 ─────────────────────────────────────────

    @staticmethod
    def _remove_value_fields(tree, class_info: ClassInfo) -> None:
        """
        @Value, @Autowired 어노테이션이 붙은 필드는
        실제 타입 의존이 아니므로 field_map에서 제거한다.
        """
        for _, fd in tree.filter(javalang.tree.FieldDeclaration):
            ann_names = {a.name for a in (fd.annotations or [])}
            if ann_names & _EXCLUDED_FIELD_ANNOTATIONS:
                for declarator in fd.declarators:
                    class_info.fields.pop(declarator.name, None)
