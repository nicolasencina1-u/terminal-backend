# app/api/v1/endpoints/camila.py

from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, distinct, or_
from sqlalchemy.orm import selectinload
import logging
from uuid import UUID
import numpy as np

from app.core.database import get_db
from app.models.camila import (
    ResultadoCamila, AsignacionGrua, CuotaCamion, MetricaGrua,
    ComparacionDetallada, ParametroCamila, FlujoModelo, 
    DistribucionBloque, EstadoProcesamiento, TipoOperacion
)

router = APIRouter()
logger = logging.getLogger(__name__)


def ensure_native_types(obj):
    """Convierte tipos numpy a tipos Python nativos"""
    if isinstance(obj, (np.bool_, np.bool8)):
        return bool(obj)
    elif isinstance(obj, (np.integer, np.int_, np.intc, np.intp, np.int8, np.int16, np.int32, np.int64,
                          np.uint8, np.uint16, np.uint32, np.uint64)):
        return int(obj)
    elif isinstance(obj, (np.floating, np.float_, np.float16, np.float32, np.float64)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, dict):
        return {k: ensure_native_types(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [ensure_native_types(v) for v in obj]
    elif isinstance(obj, tuple):
        return tuple(ensure_native_types(v) for v in obj)
    return obj


@router.get("/dashboard-ejecutivo")
async def get_dashboard_ejecutivo(
    semana: int = Query(..., ge=1, le=52, description="Número de semana"),
    dia: Optional[int] = Query(None, ge=1, le=7, description="Día de la semana"),
    turno: Optional[int] = Query(None, ge=1, le=21, description="Turno de la semana"),
    participacion: int = Query(..., ge=60, le=80, description="Porcentaje de participación"),
    hora: Optional[int] = Query(None, ge=0, le=23, description="Hora específica"),
    variant: str = Query("magdalena", description="Variante del modelo"),
    criterio: Optional[int] = Query(None, description="Criterio (2 o 3)"),
    granularidad: Optional[str] = Query(None, description="Granularidad (bahia o pila)"),
    db: AsyncSession = Depends(get_db)
):
    """
    Dashboard ejecutivo con KPIs esenciales y comparaciones modelo vs real.
    Incluye tabs: Vista General, Análisis por Bloques, Comparación Detallada, Métricas de Grúas.
    """
    
    # Construir query base
    query = select(ResultadoCamila).where(
        and_(
            ResultadoCamila.semana == semana,
            ResultadoCamila.participacion == participacion,
            ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO,
            ResultadoCamila.variant == variant
        )
    )
    
    if criterio and variant != 'magdalena':
        query = query.where(ResultadoCamila.criterio == criterio)
    if granularidad and variant != 'magdalena':
        query = query.where(ResultadoCamila.granularidad == granularidad)

    # Aplicar filtros opcionales
    if dia:
        query = query.where(ResultadoCamila.dia == dia)
    if turno:
        query = query.where(ResultadoCamila.turno == turno)
    if hora is not None:
        query = query.where(ResultadoCamila.hora_inicio == hora)
    
    # Ejecutar query
    result = await db.execute(query.order_by(ResultadoCamila.turno))
    resultados = result.scalars().all()
    
    if not resultados:
        msg_variant = f"variante '{variant}'" if variant else "modelo base"
        raise HTTPException(404, f"No hay datos para los parámetros especificados ({msg_variant})")
    
    # Si hay múltiples resultados, tomar el primero o agregar
    if len(resultados) == 1:
        resultado = resultados[0]
        return await _build_single_dashboard(resultado, db)
    else:
        return await _build_aggregated_dashboard(resultados, db)

async def _get_matriz_asignacion_gruas(resultado_id: UUID, db: AsyncSession) -> Dict:
    """Obtiene matriz de asignación de grúas por periodo y bloque"""
    
    query = select(AsignacionGrua).where(
        and_(
            AsignacionGrua.resultado_id == resultado_id,
            AsignacionGrua.asignada == True
        )
    ).order_by(AsignacionGrua.periodo, AsignacionGrua.grua_id)
    
    result = await db.execute(query)
    asignaciones = result.scalars().all()
    
    # Construir matriz
    matriz = {}
    bloques = set()
    periodos = set()
    
    for asig in asignaciones:
        bloques.add(asig.bloque_codigo)
        periodos.add(asig.periodo)
        
        if asig.periodo not in matriz:
            matriz[asig.periodo] = {}
        
        if asig.bloque_codigo not in matriz[asig.periodo]:
            matriz[asig.periodo][asig.bloque_codigo] = []
        
        matriz[asig.periodo][asig.bloque_codigo].append({
            "grua": asig.grua_id,
            "tipo": asig.tipo_equipo, # Nuevo modelo
            "movimientos": asig.movimientos_asignados,
            "activada": asig.activada
        })
    
    # Comparación con modelo ideal (distribución uniforme)
    total_gruas_periodo = {}
    for periodo in periodos:
        total_gruas_periodo[periodo] = sum(
            len(matriz.get(periodo, {}).get(bloque, []))
            for bloque in bloques
        )
    
    return {
        "matriz": matriz,
        "bloques": sorted(list(bloques)),
        "periodos": sorted(list(periodos)),
        "resumen": {
            "bloques_totales": len(bloques),
            "periodos_totales": len(periodos),
            "asignaciones_totales": len(asignaciones),
            "promedio_gruas_periodo": round(
                sum(total_gruas_periodo.values()) / len(periodos), 1
            ) if periodos else 0
        }
    }
    
async def _build_single_dashboard(resultado: ResultadoCamila, db: AsyncSession) -> Dict[str, Any]:
    """Construye dashboard para un único turno"""
    
    # Cargar datos relacionados
    distribuciones = await _get_distribuciones_bloque(resultado.id, db)
    comparaciones = await _get_comparaciones(resultado.id, db)
    metricas_gruas = await _get_metricas_gruas(resultado.id, db)
    cuotas = await _get_cuotas_periodo(resultado.id, db)
    flujos = await _get_flujos_resumen(resultado.id, db)
    
    dashboard_data = {
        "metadata": {
            "turno": resultado.turno,
            "fecha": resultado.fecha_inicio.isoformat(),
            "dia_semana": resultado.dia,
            "hora_inicio": resultado.hora_inicio,
            "participacion": resultado.participacion,
            "semana": resultado.semana
        },
        
        "tabs": {
            "vista_general": {
                "kpis_principales": {
                    "productividad": {
                        "valor": float(resultado.productividad_hora),
                        "unidad": "cont/hora",
                        "meta": 75,
                        "cumplimiento_meta": float(resultado.productividad_meta_pct),
                        "estado": "BUENO" if resultado.productividad_hora >= 75 else "BAJO"
                    },
                    "precision_modelo": {
                        "valor": float(resultado.precision_global) if resultado.precision_global else 0,
                        "unidad": "%",
                        "categoria": _categorizar_precision(resultado.precision_global),
                        "score_coincidencia": float(resultado.score_coincidencia) if resultado.score_coincidencia else 0
                    },
                    "balance_flujos": {
                        "entradas": resultado.total_entradas,
                        "salidas": resultado.total_salidas,
                        "balance": resultado.balance_flujos,
                        "categoria": resultado.balance_categoria,
                        "alerta": abs(resultado.balance_flujos) > 200
                    },
                    "ahorro_distancia": {
                        "metros": resultado.ahorro_distancia_m or 0,
                        "minutos": resultado.ahorro_tiempo_min or 0,
                        "porcentaje": float(resultado.ahorro_porcentual) if resultado.ahorro_porcentual else 0,
                        "valor_anual_usd": _calcular_valor_anual(resultado.ahorro_tiempo_min)
                    }
                },
                
                "distribucion_flujos": {
                    "carga": resultado.movimientos_carga,
                    "descarga": resultado.movimientos_descarga,
                    "entrega": resultado.movimientos_entrega,
                    "recepcion": resultado.movimientos_recepcion,
                    "total": resultado.total_movimientos_modelo,
                    "porcentajes": {
                        "carga": _calc_porcentaje(resultado.movimientos_carga, resultado.total_movimientos_modelo),
                        "descarga": _calc_porcentaje(resultado.movimientos_descarga, resultado.total_movimientos_modelo),
                        "entrega": _calc_porcentaje(resultado.movimientos_entrega, resultado.total_movimientos_modelo),
                        "recepcion": _calc_porcentaje(resultado.movimientos_recepcion, resultado.total_movimientos_modelo)
                    }
                },
                
                "utilizacion_recursos": {
                    "gruas": {
                        "total": 12,
                        "utilizadas": resultado.total_gruas_utilizadas,
                        "porcentaje": float(resultado.utilizacion_gruas_pct)
                    },
                    "bloques": {
                        "mas_utilizado": resultado.bloque_mas_utilizado,
                        "concentracion_maxima": float(resultado.concentracion_maxima_pct),
                        "distribucion_balanceada": resultado.concentracion_maxima_pct < 40
                    }
                }
            },
            
            "analisis_bloques": {
                "distribucion": distribuciones,
                "top_3_bloques": _get_top_bloques(distribuciones, 3),
                "bloques_criticos": _get_bloques_criticos(distribuciones),
                "comparacion_visual": _build_comparacion_bloques(distribuciones)
            },
            
            "comparacion_detallada": {
                "resumen": {
                    "movimientos_modelo": resultado.total_movimientos_modelo,
                    "movimientos_real": resultado.total_movimientos_real or 0,
                    "coincidencia_total": float(resultado.precision_global) if resultado.precision_global else 0,
                    "diferencias_criticas": _count_diferencias_criticas(comparaciones)
                },
                "por_tipo_flujo": _get_comparaciones_por_tipo(comparaciones, 'FLUJO'),
                "por_bloque": _get_comparaciones_por_tipo(comparaciones, 'BLOQUE'),
                "diferencias_principales": _get_principales_diferencias(comparaciones, 5)
            },
            
            "metricas_gruas": {
                "resumen": {
                    "gruas_activas": len([m for m in metricas_gruas if m['movimientos'] > 0]),
                    "productividad_promedio": _calc_productividad_promedio(metricas_gruas),
                    "balance_trabajo": _calc_balance_trabajo(metricas_gruas),
                    "cumplimiento_meta": _calc_cumplimiento_meta_gruas(metricas_gruas),
                    
                    "utilizacion_gruas": {
                        "total": 12,
                        "activas": resultado.total_gruas_utilizadas,
                        "porcentaje": float(resultado.utilizacion_gruas_pct),
                        "categoria": "OPTIMA" if resultado.utilizacion_gruas_pct >= 70 and resultado.utilizacion_gruas_pct <= 85 else "SUBOPTIMA"
                    },
                    "productividad_por_grua": {
                        "promedio": float(resultado.productividad_promedio_grua),
                        "meta": 20,
                        "cumple": resultado.productividad_promedio_grua >= 20
                    },
                    "distribucion_carga": {
                        "cv": float(resultado.distribucion_carga_cv),
                        "categoria": "EQUILIBRADA" if resultado.distribucion_carga_cv < 15 else "DESEQUILIBRADA"
                    },
                    "interferencia": {
                        "porcentaje": float(resultado.factor_interferencia_pct),
                        "nivel": "BAJO" if resultado.factor_interferencia_pct < 30 else "ALTO"
                    },
                    "reasignaciones": {
                        "porcentaje": float(resultado.indice_reasignaciones_pct),
                        "eficiencia": "ALTA" if resultado.indice_reasignaciones_pct < 10 else "BAJA"
                    },
                    "tiempo_respuesta": {
                        "sin_grua_pct": float(resultado.tiempo_respuesta_pct),
                        "alerta": resultado.tiempo_respuesta_pct > 5
                    }
                },
                "detalle_gruas": metricas_gruas,
                "distribucion_trabajo": _get_distribucion_trabajo_gruas(metricas_gruas),
                
                "matriz_asignacion": await _get_matriz_asignacion_gruas(resultado.id, db)
            },
            
            "timeline_operaciones": {
                "periodos": cuotas,
                "horas_pico": _identificar_horas_pico(cuotas),
                "utilizacion_por_hora": _calc_utilizacion_por_hora(cuotas)
            }
        }
    }
    
    return ensure_native_types(dashboard_data)


async def _build_aggregated_dashboard(resultados: List[ResultadoCamila], db: AsyncSession) -> Dict[str, Any]:
    """Construye dashboard agregado para múltiples turnos"""
    
    # Agregar métricas
    total_movimientos_modelo = sum(r.total_movimientos_modelo for r in resultados)
    total_movimientos_real = sum(r.total_movimientos_real or 0 for r in resultados)
    productividad_promedio = float(np.mean([float(r.productividad_hora) for r in resultados]))
    precision_promedio = float(np.mean([float(r.precision_global or 0) for r in resultados if r.precision_global]))
    
    # Balance agregado
    total_entradas = sum(r.total_entradas for r in resultados)
    total_salidas = sum(r.total_salidas for r in resultados)
    balance_total = total_entradas - total_salidas
    
    # Ahorro agregado
    ahorro_metros_total = sum(r.ahorro_distancia_m or 0 for r in resultados)
    ahorro_minutos_total = sum(r.ahorro_tiempo_min or 0 for r in resultados)
    
    dashboard_data = {
        "metadata": {
            "tipo": "agregado",
            "turnos_incluidos": len(resultados),
            "rango_turnos": f"{min(r.turno for r in resultados)}-{max(r.turno for r in resultados)}",
            "semana": resultados[0].semana,
            "participacion": resultados[0].participacion
        },
        
        "tabs": {
            "vista_general": {
                "kpis_principales": {
                    "productividad": {
                        "promedio": round(productividad_promedio, 2),
                        "minima": min(float(r.productividad_hora) for r in resultados),
                        "maxima": max(float(r.productividad_hora) for r in resultados),
                        "turnos_bajo_meta": len([r for r in resultados if r.productividad_hora < 75])
                    },
                    "precision_modelo": {
                        "promedio": round(precision_promedio, 2) if precision_promedio else 0,
                        "mejor_turno": _get_mejor_turno_precision(resultados),
                        "peor_turno": _get_peor_turno_precision(resultados)
                    },
                    "balance_flujos": {
                        "entradas_total": total_entradas,
                        "salidas_total": total_salidas,
                        "balance_total": balance_total,
                        "balance_promedio_turno": round(balance_total / len(resultados), 1)
                    },
                    "ahorro_acumulado": {
                        "metros_total": ahorro_metros_total,
                        "horas_total": round(ahorro_minutos_total / 60, 1),
                        "valor_proyectado_anual": _calcular_valor_anual(ahorro_minutos_total)
                    }
                },
                
                "totales": {
                    "movimientos_modelo": total_movimientos_modelo,
                    "movimientos_real": total_movimientos_real,
                    "coincidencia_global": round(
                        min(total_movimientos_modelo, total_movimientos_real) / 
                        max(total_movimientos_modelo, total_movimientos_real) * 100, 2
                    ) if max(total_movimientos_modelo, total_movimientos_real) > 0 else 0
                }
            },
            
            "evolucion_temporal": {
                "serie_productividad": [
                    {
                        "turno": r.turno,
                        "hora": r.hora_inicio,
                        "productividad": float(r.productividad_hora),
                        "cumple_meta": r.productividad_hora >= 75
                    }
                    for r in sorted(resultados, key=lambda x: x.turno)
                ],
                "serie_precision": [
                    {
                        "turno": r.turno,
                        "precision": float(r.precision_global) if r.precision_global else 0,
                        "categoria": _categorizar_precision(r.precision_global)
                    }
                    for r in sorted(resultados, key=lambda x: x.turno)
                ],
                "patrones": _identificar_patrones(resultados)
            },
            
            "analisis_comparativo": {
                "mejores_practicas": _identificar_mejores_practicas(resultados),
                "turnos_criticos": _identificar_turnos_criticos(resultados),
                "variabilidad": _calcular_variabilidad(resultados)
            }
        }
    }
    
    return ensure_native_types(dashboard_data)


# Funciones auxiliares de consulta
async def _get_distribuciones_bloque(resultado_id: UUID, db: AsyncSession) -> List[Dict]:
    """Obtiene distribuciones por bloque"""
    query = select(DistribucionBloque).where(
        DistribucionBloque.resultado_id == resultado_id
    ).order_by(DistribucionBloque.porcentaje_actividad.desc())
    
    result = await db.execute(query)
    distribuciones = result.scalars().all()
    
    return [
        {
            "bloque": d.bloque_codigo,
            "movimientos_total": d.movimientos_total,
            "porcentaje": float(d.porcentaje_actividad),
            "desglose": {
                "carga": d.movimientos_carga,
                "descarga": d.movimientos_descarga,
                "entrega": d.movimientos_entrega,
                "recepcion": d.movimientos_recepcion
            },
            "comparacion": {
                "real": d.movimientos_real or 0,
                "diferencia": d.diferencia_absoluta or 0,
                "diferencia_pct": float(d.diferencia_porcentual) if d.diferencia_porcentual else 0,
                "match": d.match_perfecto
            } if d.movimientos_real is not None else None
        }
        for d in distribuciones
    ]


async def _get_comparaciones(resultado_id: UUID, db: AsyncSession) -> List[ComparacionDetallada]:
    """Obtiene todas las comparaciones"""
    query = select(ComparacionDetallada).where(
        ComparacionDetallada.resultado_id == resultado_id
    )
    result = await db.execute(query)
    return result.scalars().all()


async def _get_metricas_gruas(resultado_id: UUID, db: AsyncSession) -> List[Dict]:
    """Obtiene métricas de grúas"""
    query = select(MetricaGrua).where(
        MetricaGrua.resultado_id == resultado_id
    ).order_by(MetricaGrua.movimientos_modelo.desc())
    
    result = await db.execute(query)
    metricas = result.scalars().all()
    
    return [
        {
            "grua_id": m.grua_id,
            "movimientos": m.movimientos_modelo,
            "movimientos_hora": float(m.movimientos_por_hora),
            "bloques_visitados": m.bloques_visitados,
            "periodos_activa": m.periodos_activa,
            "utilizacion": float(m.utilizacion_pct),
            "tiempo_productivo": float(m.tiempo_productivo_hrs),
            "tiempo_improductivo": float(m.tiempo_improductivo_hrs),
            "cumple_meta": m.cumple_meta,
            "estado": "ACTIVA" if m.movimientos_modelo > 0 else "INACTIVA"
        }
        for m in metricas
    ]


async def _get_cuotas_periodo(resultado_id: UUID, db: AsyncSession) -> List[Dict]:
    """Obtiene cuotas por periodo"""
    query = select(CuotaCamion).where(
        CuotaCamion.resultado_id == resultado_id
    ).order_by(CuotaCamion.periodo)
    
    result = await db.execute(query)
    cuotas = result.scalars().all()
    
    # Agrupar por periodo
    periodos = {}
    for c in cuotas:
        if c.periodo not in periodos:
            periodos[c.periodo] = {
                "periodo": c.periodo,
                "hora": c.hora_real,
                "cuota_total": 0,
                "capacidad_total": 0,
                "movimientos_reales": 0,
                "bloques_activos": 0,
                "gruas_totales": 0,
                "utilizacion_modelo": 0,
                "utilizacion_real": 0
            }
        
        periodos[c.periodo]["cuota_total"] += c.cuota_modelo
        periodos[c.periodo]["capacidad_total"] += c.capacidad_maxima
        periodos[c.periodo]["movimientos_reales"] += c.movimientos_reales or 0
        periodos[c.periodo]["gruas_totales"] += c.gruas_asignadas
        if c.cuota_modelo > 0:
            periodos[c.periodo]["bloques_activos"] += 1
    
    # Calcular promedios
    for p in periodos.values():
        if p["capacidad_total"] > 0:
            p["utilizacion_modelo"] = round(p["cuota_total"] / p["capacidad_total"] * 100, 1)
            p["utilizacion_real"] = round(p["movimientos_reales"] / p["capacidad_total"] * 100, 1)
    
    return list(periodos.values())


async def _get_flujos_resumen(resultado_id: UUID, db: AsyncSession) -> Dict:
    """Obtiene resumen de flujos"""
    query = select(
        FlujoModelo.tipo_flujo,
        func.sum(FlujoModelo.cantidad).label('total'),
        func.sum(FlujoModelo.cantidad_real).label('real')
    ).where(
        FlujoModelo.resultado_id == resultado_id
    ).group_by(FlujoModelo.tipo_flujo)
    
    result = await db.execute(query)
    flujos = result.all()
    
    return {
        row.tipo_flujo: {
            "modelo": row.total,
            "real": row.real or 0,
            "match": row.total == (row.real or 0)
        }
        for row in flujos
    }


# Funciones de cálculo y análisis
def _categorizar_precision(precision: float) -> str:
    """Categoriza el nivel de precisión"""
    if precision is None:
        return "SIN_DATOS"
    elif precision >= 95:
        return "EXCELENTE"
    elif precision >= 85:
        return "BUENO"
    elif precision >= 70:
        return "REGULAR"
    elif precision >= 50:
        return "BAJO"
    else:
        return "MUY_BAJO"


def _calc_porcentaje(valor: int, total: int) -> float:
    """Calcula porcentaje de forma segura"""
    return float(round((valor / total * 100), 1)) if total > 0 else 0.0


def _calcular_valor_anual(minutos_ahorrados: int) -> int:
    """Calcula valor económico anual del ahorro"""
    if not minutos_ahorrados:
        return 0
    # $50/hora * minutos * 3 turnos/día * 365 días
    return int((minutos_ahorrados / 60) * 50 * 3 * 365)


def _get_top_bloques(distribuciones: List[Dict], n: int) -> List[Dict]:
    """Obtiene los N bloques más utilizados"""
    sorted_dist = sorted(distribuciones, key=lambda x: x['movimientos_total'], reverse=True)
    return sorted_dist[:n]


def _get_bloques_criticos(distribuciones: List[Dict]) -> List[Dict]:
    """Identifica bloques con problemas o anomalías"""
    criticos = []
    for d in distribuciones:
        if d['porcentaje'] > 40:  # Muy concentrado
            criticos.append({
                "bloque": d['bloque'],
                "problema": "SOBRE_UTILIZADO",
                "porcentaje": d['porcentaje']
            })
        elif d['comparacion'] and abs(d['comparacion']['diferencia_pct']) > 50:
            criticos.append({
                "bloque": d['bloque'],
                "problema": "ALTA_DISCREPANCIA",
                "diferencia": d['comparacion']['diferencia_pct']
            })
    return criticos


def _build_comparacion_bloques(distribuciones: List[Dict]) -> Dict:
    """Construye matriz de comparación visual"""
    return {
        "bloques": [d['bloque'] for d in distribuciones],
        "modelo": [d['movimientos_total'] for d in distribuciones],
        "real": [d['comparacion']['real'] if d['comparacion'] else 0 for d in distribuciones],
        "diferencias": [d['comparacion']['diferencia'] if d['comparacion'] else 0 for d in distribuciones]
    }


def _count_diferencias_criticas(comparaciones: List[ComparacionDetallada]) -> int:
    """Cuenta diferencias críticas"""
    return len([c for c in comparaciones if c.categoria_diferencia == 'CRITICA'])


def _get_comparaciones_por_tipo(comparaciones: List[ComparacionDetallada], tipo: str) -> List[Dict]:
    """Obtiene comparaciones filtradas por tipo"""
    filtradas = [c for c in comparaciones if c.tipo_comparacion == tipo]
    return [
        {
            "subtipo": c.subtipo,
            "modelo": c.valor_modelo,
            "real": c.valor_real or 0,
            "match": c.match_exacto,
            "diferencia": c.diferencia_absoluta or 0,
            "diferencia_pct": float(c.diferencia_porcentual) if c.diferencia_porcentual else 0,
            "precision": float(c.precision_pct) if c.precision_pct else 0,
            "categoria": c.categoria_diferencia
        }
        for c in filtradas
    ]


def _get_principales_diferencias(comparaciones: List[ComparacionDetallada], n: int) -> List[Dict]:
    """Obtiene las N principales diferencias"""
    # Ordenar por diferencia absoluta
    sorted_comp = sorted(
        [c for c in comparaciones if c.diferencia_absoluta is not None],
        key=lambda x: abs(x.diferencia_absoluta),
        reverse=True
    )
    
    return [
        {
            "tipo": c.tipo_comparacion,
            "subtipo": c.subtipo,
            "diferencia": c.diferencia_absoluta,
            "diferencia_pct": float(c.diferencia_porcentual) if c.diferencia_porcentual else 0,
            "modelo": c.valor_modelo,
            "real": c.valor_real or 0,
            "requiere_accion": c.requiere_accion
        }
        for c in sorted_comp[:n]
    ]


def _calc_productividad_promedio(metricas: List[Dict]) -> float:
    """Calcula productividad promedio de grúas activas"""
    activas = [m for m in metricas if m['movimientos'] > 0]
    if not activas:
        return 0.0
    return float(round(np.mean([m['movimientos_hora'] for m in activas]), 1))


def _calc_balance_trabajo(metricas: List[Dict]) -> float:
    """Calcula balance de trabajo entre grúas (coeficiente de variación)"""
    movimientos = [m['movimientos'] for m in metricas if m['movimientos'] > 0]
    if len(movimientos) < 2:
        return 0.0
    return float(round(np.std(movimientos) / np.mean(movimientos) * 100, 1))


def _calc_cumplimiento_meta_gruas(metricas: List[Dict]) -> Dict:
    """Calcula cumplimiento de meta de grúas"""
    total_gruas = len(metricas)
    cumplen_meta = len([m for m in metricas if m['cumple_meta']])
    return {
        "cumplen": cumplen_meta,
        "no_cumplen": total_gruas - cumplen_meta,
        "porcentaje": round(cumplen_meta / total_gruas * 100, 1) if total_gruas > 0 else 0
    }


def _get_distribucion_trabajo_gruas(metricas: List[Dict]) -> Dict:
    """Analiza distribución de trabajo entre grúas"""
    movimientos = sorted([m['movimientos'] for m in metricas], reverse=True)
    total = sum(movimientos)
    
    if total == 0:
        return {"tipo": "SIN_TRABAJO", "indice_gini": 0}
    
    # Calcular índice de Gini
    n = len(movimientos)
    index = np.arange(1, n + 1)
    gini = (2 * np.sum(index * movimientos)) / (n * np.sum(movimientos)) - (n + 1) / n
    
    # Calcular 80/20
    cumsum = 0
    gruas_80 = 0
    for i, mov in enumerate(movimientos):
        cumsum += mov
        if cumsum >= total * 0.8:
            gruas_80 = i + 1
            break
    
    return {
        "tipo": "EQUITATIVA" if gini < 0.3 else "DESIGUAL" if gini > 0.6 else "MODERADA",
        "indice_gini": float(round(gini, 3)),
        "gruas_80_20": int(gruas_80),
        "porcentaje_80_20": float(round(gruas_80 / n * 100, 1))
    }


def _identificar_horas_pico(cuotas: List[Dict]) -> List[int]:
    """Identifica horas pico de operación"""
    # Ordenar por cuota total y tomar top 3
    sorted_cuotas = sorted(cuotas, key=lambda x: x['cuota_total'], reverse=True)
    return [c['hora'] for c in sorted_cuotas[:3]]


def _calc_utilizacion_por_hora(cuotas: List[Dict]) -> List[Dict]:
    """Calcula utilización por hora"""
    return [
        {
            "hora": c['hora'],
            "periodo": c['periodo'],
            "utilizacion_modelo": c['utilizacion_modelo'],
            "utilizacion_real": c['utilizacion_real'],
            "brecha": c['utilizacion_real'] - c['utilizacion_modelo']
        }
        for c in sorted(cuotas, key=lambda x: x['periodo'])
    ]


def _get_mejor_turno_precision(resultados: List[ResultadoCamila]) -> Dict:
    """Obtiene el turno con mejor precisión"""
    mejor = max(resultados, key=lambda r: r.precision_global or 0)
    return {
        "turno": mejor.turno,
        "precision": float(mejor.precision_global) if mejor.precision_global else 0,
        "hora": mejor.hora_inicio
    }


def _get_peor_turno_precision(resultados: List[ResultadoCamila]) -> Dict:
    """Obtiene el turno con peor precisión"""
    con_precision = [r for r in resultados if r.precision_global is not None]
    if not con_precision:
        return {}
    peor = min(con_precision, key=lambda r: r.precision_global)
    return {
        "turno": peor.turno,
        "precision": float(peor.precision_global),
        "hora": peor.hora_inicio
    }


def _identificar_patrones(resultados: List[ResultadoCamila]) -> Dict:
    """Identifica patrones en los datos"""
    # Agrupar por turno del día
    por_turno_dia = {}
    for r in resultados:
        td = r.turno_del_dia
        if td not in por_turno_dia:
            por_turno_dia[td] = []
        por_turno_dia[td].append(float(r.productividad_hora))
    
    # Calcular promedios
    patrones = {}
    for td, valores in por_turno_dia.items():
        patrones[f"turno_{td}"] = {
            "promedio_productividad": float(round(np.mean(valores), 1)),
            "variabilidad": float(round(np.std(valores), 1)),
            "consistencia": "ALTA" if np.std(valores) < 10 else "BAJA"
        }
    
    return patrones


def _identificar_mejores_practicas(resultados: List[ResultadoCamila]) -> List[Dict]:
    """Identifica mejores prácticas de los turnos exitosos"""
    # Top 3 turnos por productividad
    top_productividad = sorted(resultados, key=lambda r: r.productividad_hora, reverse=True)[:3]
    
    return [
        {
            "turno": r.turno,
            "productividad": float(r.productividad_hora),
            "precision": float(r.precision_global) if r.precision_global else 0,
            "balance": r.balance_flujos,
            "concentracion_maxima": float(r.concentracion_maxima_pct)
        }
        for r in top_productividad
    ]


def _identificar_turnos_criticos(resultados: List[ResultadoCamila]) -> List[Dict]:
    """Identifica turnos con problemas"""
    criticos = []
    
    for r in resultados:
        problemas = []
        if r.productividad_hora < 60:
            problemas.append("BAJA_PRODUCTIVIDAD")
        if r.precision_global and r.precision_global < 70:
            problemas.append("BAJA_PRECISION")
        if abs(r.balance_flujos) > 300:
            problemas.append("DESBALANCE_ALTO")
        
        if problemas:
            criticos.append({
                "turno": r.turno,
                "problemas": problemas,
                "productividad": float(r.productividad_hora),
                "precision": float(r.precision_global) if r.precision_global else 0
            })
    
    return criticos


def _calcular_variabilidad(resultados: List[ResultadoCamila]) -> Dict:
    """Calcula métricas de variabilidad"""
    productividades = [float(r.productividad_hora) for r in resultados]
    precisiones = [float(r.precision_global) for r in resultados if r.precision_global]
    
    return {
        "productividad": {
            "cv": float(round(np.std(productividades) / np.mean(productividades) * 100, 1)),
            "rango": float(max(productividades) - min(productividades)),
            "consistente": bool(np.std(productividades) < 15)
        },
        "precision": {
            "cv": float(round(np.std(precisiones) / np.mean(precisiones) * 100, 1)) if precisiones else 0.0,
            "rango": float(max(precisiones) - min(precisiones)) if precisiones else 0.0,
            "consistente": bool(np.std(precisiones) < 10) if precisiones else False
        }
    }


# backend/app/api/v1/endpoints/camila.py

@router.get("/filtros-disponibles")
async def get_filtros_disponibles(
    db: AsyncSession = Depends(get_db)
):
    """Obtiene valores disponibles para filtros del dashboard"""
    
    # Construir query base para filtros comunes (estado completado)
    base_filter = [ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO]

    # Obtener años únicos
    anios_result = await db.execute(
        select(distinct(ResultadoCamila.anio))
        .where(and_(*base_filter, ResultadoCamila.anio.isnot(None)))
        .order_by(ResultadoCamila.anio)
    )
    anios = anios_result.scalars().all()
    
    # Si no hay años, usar el año actual como fallback
    if not anios:
        anios = [datetime.now().year]
    
    # Obtener semanas
    semanas_result = await db.execute(
        select(distinct(ResultadoCamila.semana))
        .where(and_(*base_filter))
        .order_by(ResultadoCamila.semana)
    )
    semanas = semanas_result.scalars().all()
    
    # Obtener participaciones
    participaciones_result = await db.execute(
        select(distinct(ResultadoCamila.participacion))
        .where(and_(*base_filter))
        .order_by(ResultadoCamila.participacion)
    )
    participaciones = participaciones_result.scalars().all()
    
    # Obtener horas
    horas_result = await db.execute(
        select(distinct(ResultadoCamila.hora_inicio))
        .where(and_(*base_filter))
        .order_by(ResultadoCamila.hora_inicio)
    )
    horas = horas_result.scalars().all()

    # Obtener variantes, criterios, y granularidades
    vars_result = await db.execute(select(distinct(ResultadoCamila.variant)).where(*base_filter))
    crits_result = await db.execute(select(distinct(ResultadoCamila.criterio)).where(and_(*base_filter, ResultadoCamila.criterio.isnot(None))))
    grans_result = await db.execute(select(distinct(ResultadoCamila.granularidad)).where(and_(*base_filter, ResultadoCamila.granularidad.isnot(None))))
    
    return {
        "anios": anios,
        "semanas": semanas,
        "dias": list(range(1, 8)),  # Lunes a Domingo
        "turnos": list(range(1, 22)),  # 21 turnos por semana
        "participaciones": participaciones,
        "horas": sorted(horas),
        "variantes": [v for v in vars_result.scalars().all() if v],
        "criterios": [c for c in crits_result.scalars().all() if c],
        "granularidades": [g for g in grans_result.scalars().all() if g],
        "descripciones": {
            "dias": {
                1: "Lunes", 2: "Martes", 3: "Miércoles", 
                4: "Jueves", 5: "Viernes", 6: "Sábado", 7: "Domingo"
            },
            "turnos_del_dia": {
                1: "Mañana (08:00-16:00)",
                2: "Tarde (16:00-00:00)",
                3: "Noche (00:00-08:00)"
            }
        }
    }