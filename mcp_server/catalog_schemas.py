"""Pydantic response models for BGI catalog MCP tools."""

from __future__ import annotations

from pydantic import BaseModel, Field

# -- Tier 1 schemas --------------------------------------------------------


class GoalScore(BaseModel):
    goal: str
    score: int
    confidence: str | None = None


class MeasureSummary(BaseModel):
    id: str
    name: str
    name_nl: str | None = None
    slug: str | None = None
    category: str
    scale: str | None = None
    maintenance_level: str | None = None
    data_completeness: float | None = None
    goal_scores: list[GoalScore] = Field(default_factory=list)
    updated_at: str | None = None


class SearchMeasuresResult(BaseModel):
    measures: list[MeasureSummary]
    count: int
    last_updated: str | None = None


class PerformanceParam(BaseModel):
    parameter_key: str
    display_name: str | None = None
    value_typical: float | None = None
    value_min: float | None = None
    value_max: float | None = None
    unit: str | None = None
    confidence: str | None = None


class CostEntry(BaseModel):
    cost_type: str
    value_typical: float | None = None
    value_low: float | None = None
    value_high: float | None = None
    unit_basis: str = "per_m2"
    currency: str = "EUR"
    confidence: str | None = None


class CoBenefit(BaseModel):
    benefit_category: str
    impact_level: str
    description: str | None = None


class Constraint(BaseModel):
    constraint_type_code: str
    compatibility: str
    penalty_factor: float | None = None
    explanation: str | None = None


class SiteRequirements(BaseModel):
    min_area_m2: float | None = None
    max_slope_pct: float | None = None
    suitable_soil_types: list[str] | None = None
    max_groundwater_depth_m: float | None = None
    requires_structural_check: bool = False
    min_sun_hours_day: float | None = None
    shade_tolerance: str | None = None


class MeasureDetail(BaseModel):
    id: str
    name: str
    name_nl: str | None = None
    slug: str | None = None
    category: str
    description: str | None = None
    scale: str | None = None
    geometry_type: str | None = None
    maintenance_level: str | None = None
    typical_lifespan_years: int | None = None
    implementation_complexity: str | None = None
    data_completeness: float | None = None
    wb_nbs_family: str | None = None
    goal_scores: list[GoalScore] = Field(default_factory=list)
    performance: list[PerformanceParam] = Field(default_factory=list)
    costs: list[CostEntry] = Field(default_factory=list)
    co_benefits: list[CoBenefit] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    site_requirements: SiteRequirements | None = None
    evidence_count: int = 0
    last_updated: str | None = None


class PolicyRequirement(BaseModel):
    requirement_type: str
    requirement_text: str
    measure_name: str | None = None
    gemeente_name: str
    document_title: str
    document_date: str | None = None
    source_url: str | None = None
    subsidy_amount: float | None = None
    subsidy_unit: str | None = None
    quantitative_value: float | None = None
    quantitative_unit: str | None = None


class PolicyResult(BaseModel):
    gemeente_code: str
    gemeente_name: str
    requirements: list[PolicyRequirement]
    count: int
    last_updated: str | None = None


class MunicipalityInfo(BaseModel):
    cbs_code: str
    name: str
    province: str | None = None
    population: int | None = None
    area_km2: float | None = None


class MunicipalityResult(BaseModel):
    municipalities: list[MunicipalityInfo]
    count: int


# -- Tier 2 schemas --------------------------------------------------------


class RankedMeasure(BaseModel):
    rank: int
    measure_id: str
    name: str
    name_nl: str | None = None
    slug: str | None = None
    category: str
    final_score: float
    goal_score: float
    cost_score: float
    maintenance_level: str | None = None
    feasibility: str | None = None


class RankResult(BaseModel):
    gemeente_code: str
    gemeente_name: str
    policy_profile: str
    measures: list[RankedMeasure]
    count: int
    last_updated: str | None = None


class ContextSummary(BaseModel):
    buurt_code: str | None = None
    buurt_name: str | None = None
    wijk_code: str | None = None
    gemeente_code: str | None = None
    gemeente_name: str | None = None
    population_density: float | None = None
    dist_green_km: float | None = None
    dist_park_km: float | None = None
    dist_water_km: float | None = None
    good_health_pct: float | None = None
    overweight_pct: float | None = None
    leefbaarometer_fysiek: float | None = None
    woz_value: float | None = None
    avg_income: float | None = None
    last_updated: str | None = None


class ComparisonEntry(BaseModel):
    name: str
    slug: str | None = None
    category: str
    scale: str | None = None
    maintenance_level: str | None = None
    goal_scores: dict[str, int] = Field(default_factory=dict)
    capex_typical: float | None = None
    opex_typical: float | None = None
    retention_mm: float | None = None
    cooling_effect_deg_c: float | None = None
    infiltration_rate_mm_hr: float | None = None
    data_completeness: float | None = None


class CompareResult(BaseModel):
    measures: list[ComparisonEntry]
    count: int
    last_updated: str | None = None
