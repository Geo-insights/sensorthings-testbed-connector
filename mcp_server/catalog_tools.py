"""MCP tool implementations for BGI catalog, policy, ranking, and geography.

Each tool queries Supabase via PostgREST and returns structured results.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from mcp_server.catalog_schemas import (
    CoBenefit,
    CompareResult,
    ComparisonEntry,
    Constraint,
    ContextSummary,
    CostEntry,
    GoalScore,
    MeasureDetail,
    MeasureSummary,
    MunicipalityInfo,
    MunicipalityResult,
    PerformanceParam,
    PolicyRequirement,
    PolicyResult,
    RankedMeasure,
    RankResult,
    SearchMeasuresResult,
    SiteRequirements,
)
from mcp_server.supabase_client import SupabaseReadClient

logger = logging.getLogger(__name__)


def _error_json(error: str, message: str, fix: str) -> str:
    import json
    return json.dumps({"error": error, "message": message, "fix": fix})


# -- Tier 1 ----------------------------------------------------------------


async def search_bgi_measures(
    client: SupabaseReadClient,
    goal: str | None = None,
    category: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
) -> SearchMeasuresResult:
    """Search/filter the BGI measure catalog."""
    limit = max(1, min(limit, 50))

    # Build PostgREST query params
    params: dict[str, str] = {
        "select": "id,name,name_nl,slug,category,scale,maintenance_level,"
                  "data_completeness,updated_at,"
                  "bgi_measure_scores(goal,score,confidence)",
        "status": "eq.active",
        "limit": str(limit),
        "order": "data_completeness.desc.nullslast",
    }

    if category:
        params["category"] = f"eq.{category}"

    if keyword:
        # Search name, name_nl, and aliases
        params["or"] = (
            f"(name.ilike.*{keyword}*,"
            f"name_nl.ilike.*{keyword}*,"
            f"description.ilike.*{keyword}*)"
        )

    rows = await client.get("bgi_measures", params=params)

    measures: list[MeasureSummary] = []
    for row in rows:
        scores = [
            GoalScore(goal=s["goal"], score=s["score"], confidence=s.get("confidence"))
            for s in row.get("bgi_measure_scores", [])
        ]
        measures.append(MeasureSummary(
            id=row["id"],
            name=row["name"],
            name_nl=row.get("name_nl"),
            slug=row.get("slug"),
            category=row["category"],
            scale=row.get("scale"),
            maintenance_level=row.get("maintenance_level"),
            data_completeness=row.get("data_completeness"),
            goal_scores=scores,
            updated_at=row.get("updated_at"),
        ))

    # Sort by goal score if a goal filter is specified
    if goal and measures:
        measures.sort(
            key=lambda m: next(
                (s.score for s in m.goal_scores if s.goal == goal), 0
            ),
            reverse=True,
        )

    return SearchMeasuresResult(
        measures=measures,
        count=len(measures),
        last_updated=measures[0].updated_at if measures else None,
    )


async def get_measure_detail(
    client: SupabaseReadClient,
    measure_id_or_slug: str,
) -> MeasureDetail:
    """Full detail for one BGI measure."""
    # Try slug first, then UUID
    is_uuid = len(measure_id_or_slug) == 36 and "-" in measure_id_or_slug
    filter_key = "id" if is_uuid else "slug"
    filter_val = f"eq.{measure_id_or_slug}"

    params = {
        "select": "*,bgi_measure_scores(goal,score,confidence)",
        filter_key: filter_val,
        "status": "eq.active",
        "limit": "1",
    }
    rows = await client.get("bgi_measures", params=params)
    if not rows:
        raise ToolError(_error_json(
            "measure_not_found",
            f"No active measure found with {filter_key}='{measure_id_or_slug}'",
            "Use search_bgi_measures to find valid measure names and slugs",
        ))

    measure = rows[0]
    measure_id = measure["id"]

    # Fetch related data in parallel-ish (sequential for simplicity)
    perf_rows = await client.get("measure_performance", params={
        "measure_id": f"eq.{measure_id}",
        "select": "parameter_key,value_typical,value_min,value_max,confidence,"
                  "parameter_units(unit_symbol,display_name)",
    })
    cost_rows = await client.get("measure_costs", params={
        "measure_id": f"eq.{measure_id}",
        "select": "cost_type,value_typical,value_low,value_high,unit_basis,currency,confidence",
    })
    co_benefit_rows = await client.get("measure_co_benefits", params={
        "measure_id": f"eq.{measure_id}",
        "select": "benefit_category,impact_level,description",
    })
    constraint_rows = await client.get("measure_constraints", params={
        "measure_id": f"eq.{measure_id}",
        "select": "constraint_type_code,compatibility,penalty_factor,explanation",
    })
    site_req_rows = await client.get("measure_site_requirements", params={
        "measure_id": f"eq.{measure_id}",
        "select": "min_area_m2,max_slope_pct,suitable_soil_types,"
                  "max_groundwater_depth_m,requires_structural_check,"
                  "min_sun_hours_day,shade_tolerance",
    })
    # Count evidence
    evidence_rows = await client.get("measure_evidence", params={
        "select": "id",
        "id": f"in.({','.join(s.get('evidence_id', '') for s in perf_rows + cost_rows if s.get('evidence_id'))})" if any(s.get("evidence_id") for s in perf_rows + cost_rows) else "eq.00000000-0000-0000-0000-000000000000",
        "limit": "0",
    })

    scores = [
        GoalScore(goal=s["goal"], score=s["score"], confidence=s.get("confidence"))
        for s in measure.get("bgi_measure_scores", [])
    ]

    performance = [
        PerformanceParam(
            parameter_key=p["parameter_key"],
            display_name=(p.get("parameter_units") or {}).get("display_name"),
            value_typical=p.get("value_typical"),
            value_min=p.get("value_min"),
            value_max=p.get("value_max"),
            unit=(p.get("parameter_units") or {}).get("unit_symbol"),
            confidence=p.get("confidence"),
        )
        for p in perf_rows
    ]

    costs = [
        CostEntry(
            cost_type=c["cost_type"],
            value_typical=c.get("value_typical"),
            value_low=c.get("value_low"),
            value_high=c.get("value_high"),
            unit_basis=c.get("unit_basis", "per_m2"),
            currency=c.get("currency", "EUR"),
            confidence=c.get("confidence"),
        )
        for c in cost_rows
    ]

    co_benefits = [
        CoBenefit(
            benefit_category=cb["benefit_category"],
            impact_level=cb["impact_level"],
            description=cb.get("description"),
        )
        for cb in co_benefit_rows
    ]

    constraints = [
        Constraint(
            constraint_type_code=cn["constraint_type_code"],
            compatibility=cn["compatibility"],
            penalty_factor=cn.get("penalty_factor"),
            explanation=cn.get("explanation"),
        )
        for cn in constraint_rows
    ]

    site_reqs = None
    if site_req_rows:
        sr = site_req_rows[0]
        site_reqs = SiteRequirements(
            min_area_m2=sr.get("min_area_m2"),
            max_slope_pct=sr.get("max_slope_pct"),
            suitable_soil_types=sr.get("suitable_soil_types"),
            max_groundwater_depth_m=sr.get("max_groundwater_depth_m"),
            requires_structural_check=sr.get("requires_structural_check", False),
            min_sun_hours_day=sr.get("min_sun_hours_day"),
            shade_tolerance=sr.get("shade_tolerance"),
        )

    return MeasureDetail(
        id=measure["id"],
        name=measure["name"],
        name_nl=measure.get("name_nl"),
        slug=measure.get("slug"),
        category=measure["category"],
        description=measure.get("description"),
        scale=measure.get("scale"),
        geometry_type=measure.get("geometry_type"),
        maintenance_level=measure.get("maintenance_level"),
        typical_lifespan_years=measure.get("typical_lifespan_years"),
        implementation_complexity=measure.get("implementation_complexity"),
        data_completeness=measure.get("data_completeness"),
        wb_nbs_family=measure.get("wb_nbs_family"),
        goal_scores=scores,
        performance=performance,
        costs=costs,
        co_benefits=co_benefits,
        constraints=constraints,
        site_requirements=site_reqs,
        evidence_count=len(evidence_rows),
        last_updated=measure.get("updated_at"),
    )


async def get_policy_requirements(
    client: SupabaseReadClient,
    gemeente_code_or_name: str,
    measure_slug: str | None = None,
) -> PolicyResult:
    """Per-municipality regulatory requirements from Officiele Bekendmakingen."""
    # Resolve name to code if needed
    gemeente_code = gemeente_code_or_name
    gemeente_name = gemeente_code_or_name

    if not gemeente_code_or_name.startswith("GM"):
        # Name lookup
        muni_rows = await client.get("municipalities", schema="geo_context", params={
            "select": "cbs_code,name",
            "name": f"ilike.*{gemeente_code_or_name}*",
            "limit": "1",
        })
        if not muni_rows:
            raise ToolError(_error_json(
                "municipality_not_found",
                f"No municipality found matching '{gemeente_code_or_name}'",
                "Use lookup_municipality to find valid codes and names",
            ))
        gemeente_code = muni_rows[0]["cbs_code"]
        gemeente_name = muni_rows[0]["name"]

    params: dict[str, str] = {
        "select": "requirement_type,requirement_text,gemeente_name,"
                  "document_title,document_date,source_url,"
                  "subsidy_amount,subsidy_unit,quantitative_value,quantitative_unit,"
                  "bgi_measures(name,slug)",
        "gemeente_code": f"eq.{gemeente_code}",
        "order": "requirement_type,document_date.desc",
        "limit": "100",
    }

    if measure_slug:
        # Filter by measure via the join
        params["bgi_measures.slug"] = f"eq.{measure_slug}"

    rows = await client.get("measure_policy_context", params=params)

    requirements = [
        PolicyRequirement(
            requirement_type=r["requirement_type"],
            requirement_text=r["requirement_text"],
            measure_name=(r.get("bgi_measures") or {}).get("name"),
            gemeente_name=r.get("gemeente_name", gemeente_name),
            document_title=r["document_title"],
            document_date=r.get("document_date"),
            source_url=r.get("source_url"),
            subsidy_amount=r.get("subsidy_amount"),
            subsidy_unit=r.get("subsidy_unit"),
            quantitative_value=r.get("quantitative_value"),
            quantitative_unit=r.get("quantitative_unit"),
        )
        for r in rows
    ]

    return PolicyResult(
        gemeente_code=gemeente_code,
        gemeente_name=gemeente_name,
        requirements=requirements,
        count=len(requirements),
        last_updated=None,
    )


async def lookup_municipality(
    client: SupabaseReadClient,
    name: str | None = None,
    province: str | None = None,
) -> MunicipalityResult:
    """Find municipalities by name or province."""
    params: dict[str, str] = {
        "select": "cbs_code,name,province,population,area_km2",
        "order": "name",
        "limit": "20",
    }

    if name:
        params["name"] = f"ilike.*{name}*"
    if province:
        params["province"] = f"ilike.*{province}*"

    rows = await client.get("municipalities", schema="geo_context", params=params)

    municipalities = [
        MunicipalityInfo(
            cbs_code=r["cbs_code"],
            name=r["name"],
            province=r.get("province"),
            population=r.get("population"),
            area_km2=float(r["area_km2"]) if r.get("area_km2") else None,
        )
        for r in rows
    ]

    return MunicipalityResult(
        municipalities=municipalities,
        count=len(municipalities),
    )


# -- Tier 2 ----------------------------------------------------------------


async def rank_measures(
    client: SupabaseReadClient,
    gemeente_code: str,
    goal: str | None = None,
    policy_profile: str = "Balanced",
    limit: int = 10,
) -> RankResult:
    """Rank BGI measures for a municipality using policy-weighted scoring."""
    limit = max(1, min(limit, 50))

    # Resolve municipality name
    muni_rows = await client.get("municipalities", schema="geo_context", params={
        "select": "cbs_code,name",
        "cbs_code": f"eq.{gemeente_code}",
        "limit": "1",
    })
    if not muni_rows:
        raise ToolError(_error_json(
            "municipality_not_found",
            f"No municipality found with code '{gemeente_code}'",
            "Use lookup_municipality to find valid codes",
        ))
    gemeente_name = muni_rows[0]["name"]

    # Get policy weights
    profile_rows = await client.get("policy_profiles", params={
        "select": "id,name,policy_weights(goal,weight)",
        "name": f"eq.{policy_profile}",
        "limit": "1",
    })
    if not profile_rows:
        profile_rows = await client.get("policy_profiles", params={
            "select": "id,name,policy_weights(goal,weight)",
            "name": "eq.Balanced",
            "limit": "1",
        })

    weights: dict[str, float] = {}
    if profile_rows:
        for w in profile_rows[0].get("policy_weights", []):
            weights[w["goal"]] = float(w["weight"])

    # Boost the requested goal
    if goal and goal in weights:
        weights[goal] = weights[goal] * 1.5

    # Normalize weights
    total_w = sum(weights.values()) or 1.0
    weights = {k: v / total_w for k, v in weights.items()}

    # Get all active measures with scores and costs
    measures_rows = await client.get("bgi_measures", params={
        "select": "id,name,name_nl,slug,category,maintenance_level,"
                  "bgi_measure_scores(goal,score),"
                  "measure_costs(cost_type,value_typical)",
        "status": "eq.active",
        "limit": "100",
    })

    # Score each measure
    scored: list[dict[str, Any]] = []
    for m in measures_rows:
        scores_map: dict[str, int] = {}
        for s in m.get("bgi_measure_scores", []):
            scores_map[s["goal"]] = s["score"]

        # Weighted goal score (0-100)
        goal_score = sum(
            scores_map.get(g, 0) * w for g, w in weights.items()
        )

        # Cost score (inverse: cheaper = higher score)
        capex = next(
            (c["value_typical"] for c in m.get("measure_costs", [])
             if c["cost_type"] == "capex" and c.get("value_typical")),
            None,
        )
        cost_score = max(0.0, 100.0 - (capex or 200.0) * 0.3)

        final_score = goal_score * 0.7 + cost_score * 0.3

        scored.append({
            "measure": m,
            "goal_score": round(goal_score, 1),
            "cost_score": round(cost_score, 1),
            "final_score": round(final_score, 1),
        })

    scored.sort(key=lambda x: x["final_score"], reverse=True)
    scored = scored[:limit]

    ranked = [
        RankedMeasure(
            rank=i + 1,
            measure_id=s["measure"]["id"],
            name=s["measure"]["name"],
            name_nl=s["measure"].get("name_nl"),
            slug=s["measure"].get("slug"),
            category=s["measure"]["category"],
            final_score=s["final_score"],
            goal_score=s["goal_score"],
            cost_score=s["cost_score"],
            maintenance_level=s["measure"].get("maintenance_level"),
            feasibility="feasible",
        )
        for i, s in enumerate(scored)
    ]

    return RankResult(
        gemeente_code=gemeente_code,
        gemeente_name=gemeente_name,
        policy_profile=policy_profile,
        measures=ranked,
        count=len(ranked),
        last_updated=None,
    )


async def get_context_summary(
    client: SupabaseReadClient,
    lat: float,
    lon: float,
) -> ContextSummary:
    """Quick environmental context for a point location."""
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise ToolError(_error_json(
            "invalid_coordinates",
            f"Coordinates lat={lat}, lon={lon} are outside valid range",
            "Latitude must be -90..90, longitude -180..180",
        ))

    # Find nearest buurt by centroid bbox approximation
    # PostgREST `and` filter for range queries on the same column
    delta = 0.005  # ~500m
    buurt_rows = await client.get("neighborhoods", schema="geo_context", params={
        "select": "cbs_code,name,municipality_code,wijk_code",
        "and": (
            f"(centroid_x.gte.{lon - delta},"
            f"centroid_x.lte.{lon + delta},"
            f"centroid_y.gte.{lat - delta},"
            f"centroid_y.lte.{lat + delta})"
        ),
        "limit": "1",
    })

    if not buurt_rows:
        # Wider search ~2km
        delta = 0.02
        buurt_rows = await client.get("neighborhoods", schema="geo_context", params={
            "select": "cbs_code,name,municipality_code,wijk_code",
            "and": (
                f"(centroid_y.gte.{lat - delta},"
                f"centroid_y.lte.{lat + delta},"
                f"centroid_x.gte.{lon - delta},"
                f"centroid_x.lte.{lon + delta})"
            ),
            "limit": "1",
        })

    buurt = buurt_rows[0] if buurt_rows else {}
    buurt_code = buurt.get("cbs_code")
    gemeente_code = buurt.get("municipality_code")

    # Get buurt indicators
    indicators: dict[str, Any] = {}
    if buurt_code:
        ind_rows = await client.get("buurt_indicators", schema="geo_context", params={
            "select": "population_density,dist_green_km,dist_park_km,dist_water_km,"
                      "good_health_pct,overweight_pct,leefbaarometer_fysiek,"
                      "woz_value,avg_income",
            "buurtcode": f"eq.{buurt_code}",
            "limit": "1",
        })
        if ind_rows:
            indicators = ind_rows[0]

    # Get municipality name
    gemeente_name = None
    if gemeente_code:
        muni_rows = await client.get("municipalities", schema="geo_context", params={
            "select": "name",
            "cbs_code": f"eq.{gemeente_code}",
            "limit": "1",
        })
        if muni_rows:
            gemeente_name = muni_rows[0]["name"]

    return ContextSummary(
        buurt_code=buurt_code,
        buurt_name=buurt.get("name"),
        wijk_code=buurt.get("wijk_code"),
        gemeente_code=gemeente_code,
        gemeente_name=gemeente_name,
        population_density=indicators.get("population_density"),
        dist_green_km=indicators.get("dist_green_km"),
        dist_park_km=indicators.get("dist_park_km"),
        dist_water_km=indicators.get("dist_water_km"),
        good_health_pct=indicators.get("good_health_pct"),
        overweight_pct=indicators.get("overweight_pct"),
        leefbaarometer_fysiek=indicators.get("leefbaarometer_fysiek"),
        woz_value=indicators.get("woz_value"),
        avg_income=indicators.get("avg_income"),
    )


async def compare_measures(
    client: SupabaseReadClient,
    measure_slugs: list[str],
) -> CompareResult:
    """Side-by-side comparison of 2-5 measures."""
    if len(measure_slugs) < 2:
        raise ToolError(_error_json(
            "invalid_input",
            "At least 2 measure slugs required for comparison",
            "Provide 2-5 measure slugs",
        ))
    if len(measure_slugs) > 5:
        measure_slugs = measure_slugs[:5]

    entries: list[ComparisonEntry] = []
    for slug in measure_slugs:
        try:
            detail = await get_measure_detail(client, slug)
        except ToolError:
            continue

        # Extract key performance params
        retention = next(
            (p.value_typical for p in detail.performance
             if p.parameter_key == "retention_capacity_mm"), None
        )
        cooling = next(
            (p.value_typical for p in detail.performance
             if p.parameter_key == "cooling_effect_deg_c"), None
        )
        infiltration = next(
            (p.value_typical for p in detail.performance
             if p.parameter_key == "infiltration_rate_mm_hr"), None
        )

        # Extract costs
        capex = next(
            (c.value_typical for c in detail.costs if c.cost_type == "capex"), None
        )
        opex = next(
            (c.value_typical for c in detail.costs if c.cost_type == "opex_annual"), None
        )

        entries.append(ComparisonEntry(
            name=detail.name,
            slug=detail.slug,
            category=detail.category,
            scale=detail.scale,
            maintenance_level=detail.maintenance_level,
            goal_scores={s.goal: s.score for s in detail.goal_scores},
            capex_typical=capex,
            opex_typical=opex,
            retention_mm=retention,
            cooling_effect_deg_c=cooling,
            infiltration_rate_mm_hr=infiltration,
            data_completeness=detail.data_completeness,
        ))

    return CompareResult(
        measures=entries,
        count=len(entries),
        last_updated=None,
    )
