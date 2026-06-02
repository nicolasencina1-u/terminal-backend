# app/services/kpi_service.py

from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, case
from uuid import UUID
import logging

from app.models.camila import (
    ResultadoCamila, KPITurno, AlertaOperativa, MetricaGrua,
    FlujoModelo, AsignacionGrua, CuotaCamion, ComparacionReal,
    TipoOperacion, EstadoProcesamiento
)

logger = logging.getLogger(__name__)


class KPIService:
    """Servicio para consultar y calcular KPIs operacionales"""
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def get_dashboard_productividad(
        self, 
        anio: Optional[int] = None,
        semana: Optional[int] = None,
        dia: Optional[int] = None,
        turno: Optional[int] = None
    ) -> Dict[str, Any]:
        """Obtiene datos para el dashboard de productividad"""
        
        # Construir query base
        query = select(ResultadoCamila, KPITurno).join(
            KPITurno, ResultadoCamila.id == KPITurno.resultado_id
        ).where(
            ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
        )
        
        # Aplicar filtros
        if anio:
            query = query.where(ResultadoCamila.anio == anio)
        if semana:
            query = query.where(ResultadoCamila.semana == semana)
        if dia:
            query = query.where(ResultadoCamila.dia == dia)
        if turno:
            query = query.where(ResultadoCamila.turno == turno)
        
        # Ordenar por fecha
        query = query.order_by(ResultadoCamila.fecha_inicio.desc())
        
        result = await self.db.execute(query)
        rows = result.all()
        
        if not rows:
            return {
                "mensaje": "No hay datos para los filtros seleccionados",
                "filtros": {"anio": anio, "semana": semana, "dia": dia, "turno": turno}
            }
        
        # Si hay múltiples resultados, tomar el más reciente o promediar
        if len(rows) == 1:
            resultado, kpi = rows[0]
            
            # Datos del turno actual
            turno_actual = {
                "turno": resultado.turno,
                "fecha": resultado.fecha_inicio.isoformat(),
                "codigo": resultado.codigo,
                "productividad_hora": float(kpi.productividad_hora),
                "meta": float(kpi.productividad_meta),
                "estado": kpi.productividad_estado,
                "cumplimiento_pct": float(kpi.productividad_hora / kpi.productividad_meta * 100) if kpi.productividad_meta > 0 else 0
            }
            
            # Tendencia histórica (últimos 7 días o turnos)
            tendencia = await self._get_tendencia_productividad(resultado.fecha_inicio, 7)
            
            # Distribución por hora (períodos)
            distribucion_hora = await self._get_distribucion_por_periodo(resultado.id)
            
            # Comparación con promedio
            promedio_historico = await self._get_promedio_productividad(anio, semana)
            
            return {
                "turno_actual": turno_actual,
                "tendencia": tendencia,
                "distribucion_hora": distribucion_hora,
                "promedio_historico": promedio_historico,
                "alertas": await self._get_alertas_productividad(resultado.id)
            }
        else:
            # Agregar múltiples turnos
            return await self._agregar_productividad_multiple(rows)
    
    async def get_dashboard_balance_flujos(
        self,
        anio: Optional[int] = None,
        semana: Optional[int] = None,
        dia: Optional[int] = None,
        turno: Optional[int] = None
    ) -> Dict[str, Any]:
        """Obtiene datos para el dashboard de balance de flujos"""
        
        # Query base
        query = select(ResultadoCamila, KPITurno).join(
            KPITurno, ResultadoCamila.id == KPITurno.resultado_id
        ).where(
            ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
        )
        
        # Aplicar filtros
        if anio:
            query = query.where(ResultadoCamila.anio == anio)
        if semana:
            query = query.where(ResultadoCamila.semana == semana)
        if dia:
            query = query.where(ResultadoCamila.dia == dia)
        if turno:
            query = query.where(ResultadoCamila.turno == turno)
        
        result = await self.db.execute(query.order_by(ResultadoCamila.fecha_inicio.desc()))
        rows = result.all()
        
        if not rows:
            return {"mensaje": "No hay datos disponibles"}
        
        # Procesar resultados
        balances = []
        for resultado, kpi in rows:
            # Obtener detalles de flujos
            flujos_detalle = await self._get_flujos_detalle(resultado.id)
            
            balance_data = {
                "turno": resultado.turno,
                "fecha": resultado.fecha_inicio.isoformat(),
                "codigo": resultado.codigo,
                "resumen": {
                    "total_entradas": kpi.total_entradas,
                    "total_salidas": kpi.total_salidas,
                    "balance_neto": kpi.balance_flujos,
                    "balance_porcentual": float(kpi.balance_porcentual) if kpi.balance_porcentual else 0
                },
                "por_tipo": {
                    "recepcion": flujos_detalle.get('fr', 0),
                    "entrega": flujos_detalle.get('fe', 0),
                    "carga": flujos_detalle.get('fc', 0),
                    "descarga": flujos_detalle.get('fd', 0)
                },
                "tendencia_acumulada": await self._get_balance_acumulado(resultado.fecha_inicio)
            }
            balances.append(balance_data)
        
        # Si es un solo turno, agregar más detalles
        if len(balances) == 1:
            balance = balances[0]
            balance["por_bloque"] = await self._get_balance_por_bloque(rows[0][0].id)
            balance["proyeccion"] = await self._proyectar_balance(rows[0][0].id)
            balance["alertas"] = await self._get_alertas_balance(rows[0][0].id)
        
        return {
            "balances": balances,
            "resumen_periodo": await self._get_resumen_balance_periodo(anio, semana)
        }
    
    async def get_dashboard_utilizacion_bloques(
        self,
        anio: Optional[int] = None,
        semana: Optional[int] = None,
        dia: Optional[int] = None,
        turno: Optional[int] = None
    ) -> Dict[str, Any]:
        """Obtiene datos para el dashboard de utilización de bloques"""
        
        # Query base
        query = select(ResultadoCamila).where(
            ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
        )
        
        # Aplicar filtros
        if anio:
            query = query.where(ResultadoCamila.anio == anio)
        if semana:
            query = query.where(ResultadoCamila.semana == semana)
        if dia:
            query = query.where(ResultadoCamila.dia == dia)
        if turno:
            query = query.where(ResultadoCamila.turno == turno)
        
        result = await self.db.execute(query.order_by(ResultadoCamila.fecha_inicio.desc()).limit(1))
        resultado = result.scalar_one_or_none()
        
        if not resultado:
            return {"mensaje": "No hay datos disponibles"}
        
        # Obtener cuotas
        cuotas_result = await self.db.execute(
            select(CuotaCamion).where(CuotaCamion.resultado_id == resultado.id)
            .order_by(CuotaCamion.periodo, CuotaCamion.bloque_codigo)
        )
        cuotas = cuotas_result.scalars().all()
        
        # Agrupar por periodo
        cuotas_por_periodo = {}
        for cuota in cuotas:
            if cuota.periodo not in cuotas_por_periodo:
                cuotas_por_periodo[cuota.periodo] = []
            
            cuota_data = {
                "bloque": cuota.bloque_codigo,
                "cuota_modelo": cuota.cuota_modelo,
                "cuota_planificada": cuota.cuota_planificada,
                "movimientos_reales": cuota.movimientos_reales if cuota.movimientos_reales else 0,
                "capacidad_maxima": cuota.capacidad_maxima,
                "gruas_asignadas": cuota.gruas_asignadas,
                "cumplimiento_pct": float(cuota.cumplimiento_pct) if cuota.cumplimiento_pct else None,
                "utilizacion_real": float(cuota.utilizacion_real) if cuota.utilizacion_real else None
            }
            cuotas_por_periodo[cuota.periodo].append(cuota_data)
        
        # Calcular resumen
        total_cuota = sum(c.cuota_modelo for c in cuotas)
        total_real = sum(c.movimientos_reales for c in cuotas if c.movimientos_reales)
        total_capacidad = sum(c.capacidad_maxima for c in cuotas)
        
        return {
            "turno_info": {
                "turno": resultado.turno,
                "fecha": resultado.fecha_inicio.isoformat(),
                "codigo": resultado.codigo
            },
            "resumen": {
                "total_cuota_modelo": total_cuota,
                "total_movimientos_real": total_real,
                "total_capacidad": total_capacidad,
                "utilizacion_global": (total_real / total_capacidad * 100) if total_capacidad > 0 else 0,
                "cumplimiento_global": (total_real / total_cuota * 100) if total_cuota > 0 else 0
            },
            "cuotas_por_periodo": cuotas_por_periodo,
            "analisis_cumplimiento": await self._analizar_cumplimiento_cuotas(cuotas),
            "recomendaciones": await self._generar_recomendaciones_cuotas(cuotas)
        }
    
    async def get_dashboard_distribucion_temporal(
        self,
        anio: Optional[int] = None,
        semana: Optional[int] = None,
        dia: Optional[int] = None,
        turno: Optional[int] = None
    ) -> Dict[str, Any]:
        """Obtiene datos para el dashboard de distribución temporal"""
        
        # Query base
        query = select(ResultadoCamila).where(
            ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
        )
        
        # Aplicar filtros
        if anio:
            query = query.where(ResultadoCamila.anio == anio)
        if semana:
            query = query.where(ResultadoCamila.semana == semana)
        if dia:
            query = query.where(ResultadoCamila.dia == dia)
        if turno:
            query = query.where(ResultadoCamila.turno == turno)
        
        result = await self.db.execute(query.order_by(ResultadoCamila.fecha_inicio.desc()).limit(1))
        resultado = result.scalar_one_or_none()
        
        if not resultado:
            return {"mensaje": "No hay datos disponibles"}
        
        # Obtener flujos por periodo
        flujos_result = await self.db.execute(
            select(
                FlujoModelo.periodo,
                func.sum(FlujoModelo.cantidad).label('total')
            ).where(
                FlujoModelo.resultado_id == resultado.id
            ).group_by(FlujoModelo.periodo)
            .order_by(FlujoModelo.periodo)
        )
        
        flujos_por_periodo = {row.periodo: row.total for row in flujos_result}
        
        # Calcular distribución horaria (cada periodo = 1 hora)
        distribucion = []
        total_movimientos = sum(flujos_por_periodo.values())
        
        # Mapear periodo a hora real según el turno
        turno_del_dia = resultado.turno_del_dia
        hora_inicio = {1: 8, 2: 16, 3: 0}[turno_del_dia]
        
        for periodo in range(1, 9):
            hora_real = (hora_inicio + periodo - 1) % 24
            movimientos = flujos_por_periodo.get(periodo, 0)
            
            distribucion.append({
                "periodo": periodo,
                "hora": f"{hora_real:02d}:00",
                "movimientos": movimientos,
                "porcentaje": (movimientos / total_movimientos * 100) if total_movimientos > 0 else 0,
                "acumulado": sum(flujos_por_periodo.get(p, 0) for p in range(1, periodo + 1))
            })
        
        # Identificar picos y valles
        picos_valles = await self._identificar_picos_valles(distribucion)
        
        # Obtener comparaciones si existen
        comparaciones = await self._get_comparacion_temporal(resultado.id)
        
        return {
            "turno_info": {
                "turno": resultado.turno,
                "fecha": resultado.fecha_inicio.isoformat(),
                "codigo": resultado.codigo,
                "turno_del_dia": turno_del_dia,
                "hora_inicio": f"{hora_inicio:02d}:00"
            },
            "distribucion_temporal": distribucion,
            "estadisticas": {
                "total_movimientos": total_movimientos,
                "promedio_por_hora": total_movimientos / 8,
                "desviacion_estandar": await self._calcular_desviacion_temporal(flujos_por_periodo),
                "coeficiente_variacion": resultado.coeficiente_variacion
            },
            "picos_valles": picos_valles,
            "comparacion_real": comparaciones,
            "recomendaciones": await self._generar_recomendaciones_temporal(distribucion)
        }
    
    async def get_dashboard_alertas_operativas(
        self,
        anio: Optional[int] = None,
        semana: Optional[int] = None,
        dia: Optional[int] = None,
        turno: Optional[int] = None,
        solo_activas: bool = True
    ) -> Dict[str, Any]:
        """Obtiene el dashboard de alertas operativas"""
        
        # Query base para alertas
        query = select(AlertaOperativa, ResultadoCamila).join(
            ResultadoCamila, AlertaOperativa.resultado_id == ResultadoCamila.id
        )
        
        # Filtro de alertas activas
        if solo_activas:
            query = query.where(AlertaOperativa.activa == True)
        
        # Aplicar filtros temporales
        if anio:
            query = query.where(ResultadoCamila.anio == anio)
        if semana:
            query = query.where(ResultadoCamila.semana == semana)
        if dia:
            query = query.where(ResultadoCamila.dia == dia)
        if turno:
            query = query.where(ResultadoCamila.turno == turno)
        
        # Ordenar por severidad y fecha
        query = query.order_by(
            case(
                (AlertaOperativa.severidad == 'critica', 1),
                (AlertaOperativa.severidad == 'alta', 2),
                (AlertaOperativa.severidad == 'media', 3),
                (AlertaOperativa.severidad == 'baja', 4),
                else_=5
            ),
            AlertaOperativa.fecha_generacion.desc()
        )
        
        result = await self.db.execute(query)
        alertas_rows = result.all()
        
        # Agrupar alertas por severidad y tipo
        alertas_por_severidad = {
            'critica': [],
            'alta': [],
            'media': [],
            'baja': []
        }
        
        conteo_por_tipo = {}
        
        for alerta, resultado in alertas_rows:
            alerta_data = {
                "id": str(alerta.id),
                "tipo": alerta.tipo_alerta.value,
                "severidad": alerta.severidad,
                "titulo": alerta.titulo,
                "descripcion": alerta.descripcion,
                "fecha_generacion": alerta.fecha_generacion.isoformat(),
                "turno": resultado.turno,
                "fecha_turno": resultado.fecha_inicio.isoformat(),
                "valor_actual": float(alerta.valor_actual) if alerta.valor_actual else None,
                "valor_umbral": float(alerta.valor_umbral) if alerta.valor_umbral else None,
                "bloque": alerta.bloque_codigo,
                "grua": alerta.grua_id,
                "periodo": alerta.periodo,
                "acciones_sugeridas": alerta.acciones_sugeridas
            }
            
            alertas_por_severidad[alerta.severidad].append(alerta_data)
            
            # Contar por tipo
            tipo = alerta.tipo_alerta.value
            conteo_por_tipo[tipo] = conteo_por_tipo.get(tipo, 0) + 1
        
        # Calcular resumen
        total_alertas = len(alertas_rows)
        
        return {
            "resumen": {
                "total_alertas": total_alertas,
                "por_severidad": {
                    "critica": len(alertas_por_severidad['critica']),
                    "alta": len(alertas_por_severidad['alta']),
                    "media": len(alertas_por_severidad['media']),
                    "baja": len(alertas_por_severidad['baja'])
                },
                "por_tipo": conteo_por_tipo
            },
            "alertas": alertas_por_severidad,
            "tendencia": await self._get_tendencia_alertas(anio, semana),
            "acciones_prioritarias": await self._get_acciones_prioritarias(alertas_por_severidad)
        }
    
    # ========== MÉTODOS AUXILIARES ==========
    
    async def _get_tendencia_productividad(self, fecha_base: datetime, dias: int) -> List[Dict]:
        """Obtiene tendencia histórica de productividad"""
        fecha_inicio = fecha_base - timedelta(days=dias)
        
        query = select(ResultadoCamila, KPITurno).join(
            KPITurno, ResultadoCamila.id == KPITurno.resultado_id
        ).where(
            and_(
                ResultadoCamila.fecha_inicio >= fecha_inicio,
                ResultadoCamila.fecha_inicio <= fecha_base,
                ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
            )
        ).order_by(ResultadoCamila.fecha_inicio)
        
        result = await self.db.execute(query)
        rows = result.all()
        
        tendencia = []
        for resultado, kpi in rows:
            tendencia.append({
                "fecha": resultado.fecha_inicio.isoformat(),
                "turno": resultado.turno,
                "productividad": float(kpi.productividad_hora),
                "meta": float(kpi.productividad_meta)
            })
        
        return tendencia
    
    async def _get_distribucion_por_periodo(self, resultado_id: UUID) -> List[Dict]:
        """Obtiene distribución de movimientos por periodo"""
        query = select(
            FlujoModelo.periodo,
            func.sum(FlujoModelo.cantidad).label('total')
        ).where(
            FlujoModelo.resultado_id == resultado_id
        ).group_by(FlujoModelo.periodo).order_by(FlujoModelo.periodo)
        
        result = await self.db.execute(query)
        
        distribucion = []
        for row in result:
            distribucion.append({
                "periodo": row.periodo,
                "movimientos": row.total
            })
        
        return distribucion
    
    async def _get_promedio_productividad(self, anio: int, semana: int) -> Dict:
        """Calcula promedio de productividad para un periodo"""
        query = select(
            func.avg(KPITurno.productividad_hora).label('promedio'),
            func.min(KPITurno.productividad_hora).label('minimo'),
            func.max(KPITurno.productividad_hora).label('maximo'),
            func.count(KPITurno.id).label('total_turnos')
        ).join(
            ResultadoCamila, KPITurno.resultado_id == ResultadoCamila.id
        ).where(
            and_(
                ResultadoCamila.anio == anio,
                ResultadoCamila.semana == semana,
                ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
            )
        )
        
        result = await self.db.execute(query)
        row = result.one()
        
        return {
            "promedio": float(row.promedio) if row.promedio else 0,
            "minimo": float(row.minimo) if row.minimo else 0,
            "maximo": float(row.maximo) if row.maximo else 0,
            "total_turnos": row.total_turnos
        }
    
    async def _get_alertas_productividad(self, resultado_id: UUID) -> List[Dict]:
        """Obtiene alertas relacionadas con productividad"""
        query = select(AlertaOperativa).where(
            and_(
                AlertaOperativa.resultado_id == resultado_id,
                AlertaOperativa.tipo_alerta == TipoAlerta.PRODUCTIVIDAD_BAJA,
                AlertaOperativa.activa == True
            )
        )
        
        result = await self.db.execute(query)
        alertas = result.scalars().all()
        
        return [
            {
                "titulo": a.titulo,
                "severidad": a.severidad,
                "acciones": a.acciones_sugeridas
            }
            for a in alertas
        ]
    
    async def _agregar_productividad_multiple(self, rows: List) -> Dict:
        """Agrega productividad de múltiples turnos"""
        productividades = []
        total_movimientos = 0
        
        for resultado, kpi in rows:
            productividades.append(float(kpi.productividad_hora))
            total_movimientos += resultado.total_movimientos_modelo
        
        promedio = sum(productividades) / len(productividades) if productividades else 0
        
        return {
            "resumen_agregado": {
                "total_turnos": len(rows),
                "productividad_promedio": promedio,
                "total_movimientos": total_movimientos,
                "rango_fechas": {
                    "desde": rows[-1][0].fecha_inicio.isoformat(),
                    "hasta": rows[0][0].fecha_inicio.isoformat()
                }
            },
            "detalle_turnos": [
                {
                    "turno": r[0].turno,
                    "fecha": r[0].fecha_inicio.isoformat(),
                    "productividad": float(r[1].productividad_hora),
                    "estado": r[1].productividad_estado
                }
                for r in rows
            ]
        }
    
    async def _get_flujos_detalle(self, resultado_id: UUID) -> Dict[str, int]:
        """Obtiene detalle de flujos por tipo"""
        query = select(
            FlujoModelo.tipo_flujo,
            func.sum(FlujoModelo.cantidad).label('total')
        ).where(
            FlujoModelo.resultado_id == resultado_id
        ).group_by(FlujoModelo.tipo_flujo)
        
        result = await self.db.execute(query)
        
        return {row.tipo_flujo: row.total for row in result}
    
    async def _get_balance_acumulado(self, fecha_base: datetime) -> List[Dict]:
        """Calcula balance acumulado de los últimos días"""
        fecha_inicio = fecha_base - timedelta(days=7)
        
        query = select(ResultadoCamila, KPITurno).join(
            KPITurno, ResultadoCamila.id == KPITurno.resultado_id
        ).where(
            and_(
                ResultadoCamila.fecha_inicio >= fecha_inicio,
                ResultadoCamila.fecha_inicio <= fecha_base,
                ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
            )
        ).order_by(ResultadoCamila.fecha_inicio)
        
        result = await self.db.execute(query)
        rows = result.all()
        
        balance_acumulado = []
        acumulado = 0
        
        for resultado, kpi in rows:
            acumulado += kpi.balance_flujos
            balance_acumulado.append({
                "fecha": resultado.fecha_inicio.isoformat(),
                "turno": resultado.turno,
                "balance_turno": kpi.balance_flujos,
                "balance_acumulado": acumulado
            })
        
        return balance_acumulado
    
    async def _get_balance_por_bloque(self, resultado_id: UUID) -> Dict[str, Dict]:
        """Obtiene balance de flujos por bloque"""
        query = select(
            FlujoModelo.bloque_codigo,
            FlujoModelo.tipo_flujo,
            func.sum(FlujoModelo.cantidad).label('total')
        ).where(
            FlujoModelo.resultado_id == resultado_id
        ).group_by(FlujoModelo.bloque_codigo, FlujoModelo.tipo_flujo)
        
        result = await self.db.execute(query)
        
        # Agrupar por bloque
        balance_bloques = {}
        for row in result:
            bloque = row.bloque_codigo
            if bloque not in balance_bloques:
                balance_bloques[bloque] = {
                    'entradas': 0,
                    'salidas': 0,
                    'balance': 0
                }
            
            # fr y fd son entradas, fc y fe son salidas
            if row.tipo_flujo in ['fr', 'fd']:
                balance_bloques[bloque]['entradas'] += row.total
            else:
                balance_bloques[bloque]['salidas'] += row.total
        
        # Calcular balance neto
        for bloque, datos in balance_bloques.items():
            datos['balance'] = datos['entradas'] - datos['salidas']
        
        return balance_bloques
    
    async def _proyectar_balance(self, resultado_id: UUID) -> Dict:
        """Proyecta el balance para los próximos turnos"""
        # Obtener KPI actual
        kpi_result = await self.db.execute(
            select(KPITurno).where(KPITurno.resultado_id == resultado_id)
        )
        kpi = kpi_result.scalar_one_or_none()
        
        if not kpi:
            return {}
        
        # Proyección simple basada en tendencia
        return {
            "balance_actual": kpi.balance_flujos,
            "proyeccion_proximo_turno": kpi.balance_flujos * 0.8,  # Asume 20% de reducción
            "turnos_para_equilibrio": abs(kpi.balance_flujos) // 100 if kpi.balance_flujos != 0 else 0,
            "recomendacion": "Aumentar salidas" if kpi.balance_flujos > 0 else "Aumentar entradas"
        }
    
    async def _get_alertas_balance(self, resultado_id: UUID) -> List[Dict]:
        """Obtiene alertas de balance"""
        query = select(AlertaOperativa).where(
            and_(
                AlertaOperativa.resultado_id == resultado_id,
                AlertaOperativa.tipo_alerta == TipoAlerta.DESBALANCE_FLUJOS,
                AlertaOperativa.activa == True
            )
        )
        
        result = await self.db.execute(query)
        return [
            {
                "titulo": a.titulo,
                "severidad": a.severidad,
                "valor": float(a.valor_actual) if a.valor_actual else 0
            }
            for a in result.scalars()
        ]
    
    async def _get_resumen_balance_periodo(self, anio: int, semana: int) -> Dict:
        """Resume balance del periodo"""
        query = select(
            func.sum(KPITurno.total_entradas).label('total_entradas'),
            func.sum(KPITurno.total_salidas).label('total_salidas'),
            func.avg(KPITurno.balance_flujos).label('balance_promedio')
        ).join(
            ResultadoCamila, KPITurno.resultado_id == ResultadoCamila.id
        ).where(
            and_(
                ResultadoCamila.anio == anio,
                ResultadoCamila.semana == semana,
                ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
            )
        )
        
        result = await self.db.execute(query)
        row = result.one()
        
        return {
            "total_entradas_semana": row.total_entradas or 0,
            "total_salidas_semana": row.total_salidas or 0,
            "balance_neto_semana": (row.total_entradas or 0) - (row.total_salidas or 0),
            "balance_promedio_turno": float(row.balance_promedio) if row.balance_promedio else 0
        }
    
    async def _get_utilizacion_bloques(self, resultado_id: UUID) -> List[Dict]:
        """Calcula utilización por bloque"""
        # Obtener flujos por bloque
        query = select(
            FlujoModelo.bloque_codigo,
            func.sum(FlujoModelo.cantidad).label('total')
        ).where(
            FlujoModelo.resultado_id == resultado_id
        ).group_by(FlujoModelo.bloque_codigo)
        
        result = await self.db.execute(query)
        
        # Calcular porcentajes
        flujos_bloque = {row.bloque_codigo: row.total for row in result}
        total = sum(flujos_bloque.values())
        
        utilizacion = []
        for bloque in ['C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9']:
            movimientos = flujos_bloque.get(bloque, 0)
            utilizacion.append({
                "bloque": bloque,
                "movimientos": movimientos,
                "porcentaje": (movimientos / total * 100) if total > 0 else 0,
                "estado": self._clasificar_utilizacion_bloque(movimientos, total)
            })
        
        return sorted(utilizacion, key=lambda x: x['movimientos'], reverse=True)
    
    def _clasificar_utilizacion_bloque(self, movimientos: int, total: int) -> str:
        """Clasifica el estado de utilización de un bloque"""
        if total == 0:
            return "inactivo"
        
        porcentaje = (movimientos / total) * 100
        
        if porcentaje > 40:
            return "critico"
        elif porcentaje > 30:
            return "alto"
        elif porcentaje > 10:
            return "normal"
        elif porcentaje > 0:
            return "bajo"
        else:
            return "inactivo"
    
    async def _get_mapa_calor_bloques(self, resultado_id: UUID) -> Dict[str, List[Dict]]:
        """Genera mapa de calor por periodo-bloque"""
        query = select(
            FlujoModelo.periodo,
            FlujoModelo.bloque_codigo,
            func.sum(FlujoModelo.cantidad).label('total')
        ).where(
            FlujoModelo.resultado_id == resultado_id
        ).group_by(FlujoModelo.periodo, FlujoModelo.bloque_codigo)
        
        result = await self.db.execute(query)
        
        # Crear matriz
        mapa = {}
        max_valor = 0
        
        for row in result:
            if row.bloque_codigo not in mapa:
                mapa[row.bloque_codigo] = {}
            mapa[row.bloque_codigo][row.periodo] = row.total
            max_valor = max(max_valor, row.total)
        
        # Formatear para visualización
        mapa_calor = []
        for bloque in ['C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9']:
            periodos_data = []
            for periodo in range(1, 9):
                valor = mapa.get(bloque, {}).get(periodo, 0)
                intensidad = (valor / max_valor) if max_valor > 0 else 0
                periodos_data.append({
                    "periodo": periodo,
                    "valor": valor,
                    "intensidad": intensidad
                })
            
            mapa_calor.append({
                "bloque": bloque,
                "periodos": periodos_data
            })
        
        return {
            "mapa": mapa_calor,
            "max_valor": max_valor
        }
    
    async def _comparar_utilizacion_turnos(self, resultado_actual: ResultadoCamila) -> Dict:
        """Compara utilización entre turnos del día"""
        # Obtener otros turnos del mismo día
        query = select(ResultadoCamila, KPITurno).join(
            KPITurno, ResultadoCamila.id == KPITurno.resultado_id
        ).where(
            and_(
                ResultadoCamila.anio == resultado_actual.anio,
                ResultadoCamila.semana == resultado_actual.semana,
                ResultadoCamila.dia == resultado_actual.dia,
                ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
            )
        ).order_by(ResultadoCamila.turno_del_dia)
        
        result = await self.db.execute(query)
        rows = result.all()
        
        comparacion = []
        for resultado, kpi in rows:
            comparacion.append({
                "turno_del_dia": resultado.turno_del_dia,
                "turno": resultado.turno,
                "bloques_activos": resultado.total_bloques_visitados,
                "bloque_mas_activo": kpi.bloque_mas_activo,
                "concentracion_maxima": float(kpi.bloque_mas_activo_pct) if kpi.bloque_mas_activo_pct else 0
            })
        
        return {
            "turnos": comparacion,
            "turno_actual": resultado_actual.turno_del_dia
        }
    
    async def _analizar_mix_operaciones(self, mix_data: List[Dict]) -> Dict:
        """Analiza el mix de operaciones"""
        if not mix_data:
            return {}
        
        # Identificar desbalances
        ultimo_mix = mix_data[0]["distribucion"]
        
        desbalances = []
        if abs(ultimo_mix["recepcion"]["porcentaje"] - ultimo_mix["entrega"]["porcentaje"]) > 20:
            desbalances.append("Desbalance entre recepciones y entregas")
        
        if abs(ultimo_mix["carga"]["porcentaje"] - ultimo_mix["descarga"]["porcentaje"]) > 20:
            desbalances.append("Desbalance entre cargas y descargas")
        
        # Calcular variabilidad
        if len(mix_data) > 1:
            variabilidad = {
                "recepcion": max(m["distribucion"]["recepcion"]["porcentaje"] for m in mix_data) - 
                            min(m["distribucion"]["recepcion"]["porcentaje"] for m in mix_data),
                "entrega": max(m["distribucion"]["entrega"]["porcentaje"] for m in mix_data) - 
                          min(m["distribucion"]["entrega"]["porcentaje"] for m in mix_data),
                "carga": max(m["distribucion"]["carga"]["porcentaje"] for m in mix_data) - 
                        min(m["distribucion"]["carga"]["porcentaje"] for m in mix_data),
                "descarga": max(m["distribucion"]["descarga"]["porcentaje"] for m in mix_data) - 
                           min(m["distribucion"]["descarga"]["porcentaje"] for m in mix_data)
            }
        else:
            variabilidad = {}
        
        return {
            "desbalances": desbalances,
            "variabilidad": variabilidad,
            "recomendacion": "Ajustar programación para balancear operaciones" if desbalances else "Mix balanceado"
        }
    
    def _clasificar_estado_grua(self, utilizacion: float) -> str:
        """Clasifica el estado de una grúa según su utilización"""
        if utilizacion >= 80:
            return "sobrecargada"
        elif utilizacion >= 60:
            return "optima"
        elif utilizacion >= 40:
            return "normal"
        elif utilizacion >= 20:
            return "subutilizada"
        else:
            return "idle"
    
    async def _get_distribucion_trabajo_gruas(self, resultado_id: UUID) -> Dict:
        """Obtiene distribución de trabajo entre grúas"""
        query = select(
            AsignacionGrua.grua_id,
            AsignacionGrua.bloque_codigo,
            func.count().label('asignaciones')
        ).where(
            and_(
                AsignacionGrua.resultado_id == resultado_id,
                AsignacionGrua.asignada == True
            )
        ).group_by(AsignacionGrua.grua_id, AsignacionGrua.bloque_codigo)
        
        result = await self.db.execute(query)
        
        distribucion = {}
        for row in result:
            if row.grua_id not in distribucion:
                distribucion[row.grua_id] = []
            distribucion[row.grua_id].append({
                "bloque": row.bloque_codigo,
                "asignaciones": row.asignaciones
            })
        
        return distribucion
    
    async def _generar_recomendaciones_gruas(self, gruas_data: List[Dict]) -> List[str]:
        """Genera recomendaciones para optimizar grúas"""
        recomendaciones = []
        
        # Identificar grúas subutilizadas
        subutilizadas = [g for g in gruas_data if g["estado"] in ["subutilizada", "idle"]]
        if subutilizadas:
            recomendaciones.append(
                f"Reasignar {len(subutilizadas)} grúas subutilizadas a bloques con mayor demanda"
            )
        
        # Identificar grúas sobrecargadas
        sobrecargadas = [g for g in gruas_data if g["estado"] == "sobrecargada"]
        if sobrecargadas:
            recomendaciones.append(
                f"Balancear carga de {len(sobrecargadas)} grúas sobrecargadas"
            )
        
        # Verificar distribución
        utilizaciones = [g["utilizacion_pct"] for g in gruas_data if g["movimientos"] > 0]
        if utilizaciones:
            cv = (np.std(utilizaciones) / np.mean(utilizaciones)) * 100 if np.mean(utilizaciones) > 0 else 0
            if cv > 30:
                recomendaciones.append("Alta variabilidad en utilización: redistribuir asignaciones")
        
        return recomendaciones
    
    async def _analizar_precision_modelo(self, resultado: ResultadoCamila, comparaciones: List) -> Dict:
        """Analiza la precisión del modelo"""
        analisis = {
            "precision_general": "excelente" if resultado.accuracy_global > 90 else 
                               "buena" if resultado.accuracy_global > 80 else
                               "aceptable" if resultado.accuracy_global > 70 else "baja",
            "areas_mejora": [],
            "fortalezas": []
        }
        
        # Analizar por tipo de comparación
        for comp in comparaciones:
            if comp.tipo_comparacion == 'por_bloque' and comp.accuracy < 80:
                analisis["areas_mejora"].append(f"Bloque {comp.dimension}: accuracy {comp.accuracy:.1f}%")
            elif comp.tipo_comparacion == 'por_bloque' and comp.accuracy > 95:
                analisis["fortalezas"].append(f"Bloque {comp.dimension}: excelente predicción")
        
        return analisis
    
    async def _analizar_cumplimiento_cuotas(self, cuotas: List[CuotaCamion]) -> Dict:
        """Analiza cumplimiento de cuotas"""
        total_cuotas = len(cuotas)
        cumplidas = sum(1 for c in cuotas if c.cumplimiento_pct and c.cumplimiento_pct >= 90)
        incumplidas = sum(1 for c in cuotas if c.cumplimiento_pct and c.cumplimiento_pct < 70)
        
        return {
            "total_cuotas": total_cuotas,
            "cumplidas": cumplidas,
            "parcialmente_cumplidas": total_cuotas - cumplidas - incumplidas,
            "incumplidas": incumplidas,
            "tasa_cumplimiento": (cumplidas / total_cuotas * 100) if total_cuotas > 0 else 0
        }
    
    async def _generar_recomendaciones_cuotas(self, cuotas: List[CuotaCamion]) -> List[str]:
        """Genera recomendaciones para cuotas"""
        recomendaciones = []
        
        # Identificar bloques con baja utilización
        for cuota in cuotas:
            if cuota.utilizacion_real and cuota.utilizacion_real < 50 and cuota.gruas_asignadas > 0:
                recomendaciones.append(
                    f"Bloque {cuota.bloque_codigo} periodo {cuota.periodo}: "
                    f"utilización {cuota.utilizacion_real:.0f}% - considerar reasignar grúas"
                )
        
        return recomendaciones[:3]  # Limitar a 3 recomendaciones principales
    
    async def _identificar_picos_valles(self, distribucion: List[Dict]) -> Dict:
        """Identifica picos y valles en la distribución temporal"""
        movimientos = [d["movimientos"] for d in distribucion]
        promedio = sum(movimientos) / len(movimientos) if movimientos else 0
        
        picos = []
        valles = []
        
        for d in distribucion:
            if d["movimientos"] > promedio * 1.3:  # 30% sobre el promedio
                picos.append({
                    "periodo": d["periodo"],
                    "hora": d["hora"],
                    "movimientos": d["movimientos"],
                    "exceso_pct": ((d["movimientos"] - promedio) / promedio * 100)
                })
            elif d["movimientos"] < promedio * 0.7:  # 30% bajo el promedio
                valles.append({
                    "periodo": d["periodo"],
                    "hora": d["hora"],
                    "movimientos": d["movimientos"],
                    "deficit_pct": ((promedio - d["movimientos"]) / promedio * 100)
                })
        
        return {
            "picos": picos,
            "valles": valles,
            "promedio": promedio
        }
    
    async def _get_comparacion_temporal(self, resultado_id: UUID) -> Dict:
        """Obtiene comparación temporal con datos reales"""
        query = select(ComparacionReal).where(
            and_(
                ComparacionReal.resultado_id == resultado_id,
                ComparacionReal.tipo_comparacion == 'por_periodo'
            )
        ).order_by(ComparacionReal.dimension)
        
        result = await self.db.execute(query)
        comparaciones = result.scalars().all()
        
        if not comparaciones:
            return {}
        
        return {
            "periodos": [
                {
                    "periodo": int(c.dimension),
                    "modelo": float(c.valor_modelo),
                    "real": float(c.valor_real),
                    "accuracy": float(c.accuracy)
                }
                for c in comparaciones
            ]
        }
    
    async def _calcular_desviacion_temporal(self, flujos_por_periodo: Dict[int, int]) -> float:
        """Calcula desviación estándar de la distribución temporal"""
        valores = list(flujos_por_periodo.values())
        if len(valores) > 1:
            return float(np.std(valores))
        return 0.0
    
    async def _generar_recomendaciones_temporal(self, distribucion: List[Dict]) -> List[str]:
        """Genera recomendaciones para distribución temporal"""
        recomendaciones = []
        
        # Identificar periodos críticos
        for d in distribucion:
            if d["porcentaje"] > 20:
                recomendaciones.append(
                    f"Alto volumen en {d['hora']}: considerar refuerzo de grúas"
                )
            elif d["porcentaje"] < 5 and d["periodo"] not in [1, 8]:
                recomendaciones.append(
                    f"Bajo volumen en {d['hora']}: oportunidad para mantenimiento"
                )
        
        return recomendaciones[:3]
    
    async def _get_tendencia_alertas(self, anio: int, semana: int) -> Dict:
        """Obtiene tendencia de alertas por tipo"""
        # Query para contar alertas por tipo y severidad en el periodo
        query = select(
            AlertaOperativa.tipo_alerta,
            AlertaOperativa.severidad,
            func.count().label('cantidad')
        ).join(
            ResultadoCamila, AlertaOperativa.resultado_id == ResultadoCamila.id
        ).where(
            and_(
                ResultadoCamila.anio == anio,
                ResultadoCamila.semana == semana
            )
        ).group_by(AlertaOperativa.tipo_alerta, AlertaOperativa.severidad)
        
        result = await self.db.execute(query)
        
        tendencia = {}
        for row in result:
            tipo = row.tipo_alerta.value
            if tipo not in tendencia:
                tendencia[tipo] = {"critica": 0, "alta": 0, "media": 0, "baja": 0}
            tendencia[tipo][row.severidad] = row.cantidad
        
        return tendencia
    
    async def _get_acciones_prioritarias(self, alertas_por_severidad: Dict) -> List[Dict]:
        """Extrae acciones prioritarias de las alertas críticas y altas"""
        acciones = []
        
        # Priorizar alertas críticas
        for alerta in alertas_por_severidad.get('critica', []):
            if alerta['acciones_sugeridas']:
                acciones.append({
                    "prioridad": "critica",
                    "tipo": alerta['tipo'],
                    "accion": alerta['acciones_sugeridas'][0],  # Primera acción sugerida
                    "contexto": alerta['titulo']
                })
        
        # Luego alertas altas
        for alerta in alertas_por_severidad.get('alta', []):
            if alerta['acciones_sugeridas'] and len(acciones) < 5:
                acciones.append({
                    "prioridad": "alta",
                    "tipo": alerta['tipo'],
                    "accion": alerta['acciones_sugeridas'][0],
                    "contexto": alerta['titulo']
                })
        
        return acciones[:5]  # Máximo 5 acciones prioritariasone_or_none()
        
        if not resultado:
            return {"mensaje": "No hay datos disponibles"}
        
        # Obtener utilización por bloque
        utilizacion_bloques = await self._get_utilizacion_bloques(resultado.id)
        
        # Obtener mapa de calor por periodo-bloque
        mapa_calor = await self._get_mapa_calor_bloques(resultado.id)
        
        # Obtener KPI del turno
        kpi_result = await self.db.execute(
            select(KPITurno).where(KPITurno.resultado_id == resultado.id)
        )
        kpi = kpi_result.scalar_one_or_none()
        
        return {
            "turno_info": {
                "turno": resultado.turno,
                "fecha": resultado.fecha_inicio.isoformat(),
                "codigo": resultado.codigo
            },
            "utilizacion_bloques": utilizacion_bloques,
            "mapa_calor": mapa_calor,
            "bloque_critico": {
                "codigo": kpi.bloque_mas_activo if kpi else None,
                "porcentaje": float(kpi.bloque_mas_activo_pct) if kpi else 0,
                "recomendacion": "Redistribuir carga a bloques adyacentes" if kpi and kpi.bloque_mas_activo_pct > 40 else "Distribución balanceada"
            },
            "comparacion_turnos": await self._comparar_utilizacion_turnos(resultado)
        }
    
    async def get_dashboard_mix_operaciones(
        self,
        anio: Optional[int] = None,
        semana: Optional[int] = None,
        dia: Optional[int] = None,
        turno: Optional[int] = None
    ) -> Dict[str, Any]:
        """Obtiene datos para el dashboard de mix de operaciones"""
        
        # Query base
        query = select(ResultadoCamila).where(
            ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
        )
        
        # Aplicar filtros
        if anio:
            query = query.where(ResultadoCamila.anio == anio)
        if semana:
            query = query.where(ResultadoCamila.semana == semana)
        if dia:
            query = query.where(ResultadoCamila.dia == dia)
        if turno:
            query = query.where(ResultadoCamila.turno == turno)
        
        result = await self.db.execute(query.order_by(ResultadoCamila.fecha_inicio.desc()))
        resultados = result.scalars().all()
        
        if not resultados:
            return {"mensaje": "No hay datos disponibles"}
        
        mix_data = []
        
        for resultado in resultados[:5]:  # Limitar a últimos 5 turnos
            # Obtener flujos agrupados por tipo
            flujos_result = await self.db.execute(
                select(
                    FlujoModelo.tipo_operacion,
                    func.sum(FlujoModelo.cantidad).label('total')
                ).where(
                    FlujoModelo.resultado_id == resultado.id
                ).group_by(FlujoModelo.tipo_operacion)
            )
            flujos_por_tipo = {row.tipo_operacion.value: row.total for row in flujos_result}
            
            total = sum(flujos_por_tipo.values())
            
            mix_turno = {
                "turno": resultado.turno,
                "fecha": resultado.fecha_inicio.isoformat(),
                "total_movimientos": total,
                "distribucion": {
                    "recepcion": {
                        "cantidad": flujos_por_tipo.get('recepcion', 0),
                        "porcentaje": flujos_por_tipo.get('recepcion', 0) / total * 100 if total > 0 else 0
                    },
                    "entrega": {
                        "cantidad": flujos_por_tipo.get('entrega', 0),
                        "porcentaje": flujos_por_tipo.get('entrega', 0) / total * 100 if total > 0 else 0
                    },
                    "carga": {
                        "cantidad": flujos_por_tipo.get('carga', 0),
                        "porcentaje": flujos_por_tipo.get('carga', 0) / total * 100 if total > 0 else 0
                    },
                    "descarga": {
                        "cantidad": flujos_por_tipo.get('descarga', 0),
                        "porcentaje": flujos_por_tipo.get('descarga', 0) / total * 100 if total > 0 else 0
                    }
                }
            }
            mix_data.append(mix_turno)
        
        # Calcular promedio del período
        if mix_data:
            promedio = {
                "recepcion": sum(m["distribucion"]["recepcion"]["porcentaje"] for m in mix_data) / len(mix_data),
                "entrega": sum(m["distribucion"]["entrega"]["porcentaje"] for m in mix_data) / len(mix_data),
                "carga": sum(m["distribucion"]["carga"]["porcentaje"] for m in mix_data) / len(mix_data),
                "descarga": sum(m["distribucion"]["descarga"]["porcentaje"] for m in mix_data) / len(mix_data)
            }
        else:
            promedio = {"recepcion": 0, "entrega": 0, "carga": 0, "descarga": 0}
        
        return {
            "mix_operaciones": mix_data,
            "promedio_periodo": promedio,
            "analisis": await self._analizar_mix_operaciones(mix_data)
        }
    
    async def get_dashboard_productividad_gruas(
        self,
        anio: Optional[int] = None,
        semana: Optional[int] = None,
        dia: Optional[int] = None,
        turno: Optional[int] = None
    ) -> Dict[str, Any]:
        """Obtiene datos para el dashboard de productividad por grúa"""
        
        # Query base
        query = select(ResultadoCamila).where(
            ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
        )
        
        # Aplicar filtros
        if anio:
            query = query.where(ResultadoCamila.anio == anio)
        if semana:
            query = query.where(ResultadoCamila.semana == semana)
        if dia:
            query = query.where(ResultadoCamila.dia == dia)
        if turno:
            query = query.where(ResultadoCamila.turno == turno)
        
        result = await self.db.execute(query.order_by(ResultadoCamila.fecha_inicio.desc()).limit(1))
        resultado = result.scalar_one_or_none()
        
        if not resultado:
            return {"mensaje": "No hay datos disponibles"}
        
        # Obtener métricas de grúas
        metricas_result = await self.db.execute(
            select(MetricaGrua).where(MetricaGrua.resultado_id == resultado.id)
            .order_by(MetricaGrua.grua_id)
        )
        metricas = metricas_result.scalars().all()
        
        gruas_data = []
        total_movimientos = 0
        gruas_activas = 0
        
        for metrica in metricas:
            if metrica.movimientos_modelo > 0:
                gruas_activas += 1
            
            total_movimientos += metrica.movimientos_modelo
            
            grua_info = {
                "grua_id": metrica.grua_id,
                "movimientos": metrica.movimientos_modelo,
                "productividad_hora": metrica.movimientos_modelo / 8,  # Turno de 8 horas
                "utilizacion_pct": float(metrica.utilizacion_pct),
                "tiempo_productivo_hrs": float(metrica.tiempo_productivo_hrs),
                "tiempo_improductivo_hrs": float(metrica.tiempo_improductivo_hrs),
                "bloques_visitados": metrica.bloques_visitados,
                "estado": self._clasificar_estado_grua(float(metrica.utilizacion_pct))
            }
            gruas_data.append(grua_info)
        
        # Calcular estadísticas
        if gruas_data:
            productividades = [g["productividad_hora"] for g in gruas_data if g["movimientos"] > 0]
            promedio_productividad = sum(productividades) / len(productividades) if productividades else 0
            max_productividad = max(productividades) if productividades else 0
            min_productividad = min(productividades) if productividades else 0
        else:
            promedio_productividad = max_productividad = min_productividad = 0
        
        return {
            "turno_info": {
                "turno": resultado.turno,
                "fecha": resultado.fecha_inicio.isoformat(),
                "codigo": resultado.codigo
            },
            "resumen": {
                "total_gruas": len(metricas),
                "gruas_activas": gruas_activas,
                "total_movimientos": total_movimientos,
                "promedio_productividad": promedio_productividad,
                "max_productividad": max_productividad,
                "min_productividad": min_productividad
            },
            "gruas": gruas_data,
            "distribucion": await self._get_distribucion_trabajo_gruas(resultado.id),
            "recomendaciones": await self._generar_recomendaciones_gruas(gruas_data)
        }
    
    async def get_dashboard_precision_modelo(
        self,
        anio: Optional[int] = None,
        semana: Optional[int] = None,
        dia: Optional[int] = None,
        turno: Optional[int] = None
    ) -> Dict[str, Any]:
        """Obtiene datos para el dashboard de precisión del modelo"""
        
        # Query base
        query = select(ResultadoCamila).where(
            and_(
                ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO,
                ResultadoCamila.accuracy_global.isnot(None)
            )
        )
        
        # Aplicar filtros
        if anio:
            query = query.where(ResultadoCamila.anio == anio)
        if semana:
            query = query.where(ResultadoCamila.semana == semana)
        if dia:
            query = query.where(ResultadoCamila.dia == dia)
        if turno:
            query = query.where(ResultadoCamila.turno == turno)
        
        result = await self.db.execute(query.order_by(ResultadoCamila.fecha_inicio.desc()))
        resultados = result.scalars().all()
        
        if not resultados:
            return {"mensaje": "No hay datos de comparación modelo vs real disponibles"}
        
        # Datos del turno más reciente
        ultimo = resultados[0]
        
        # Obtener comparaciones detalladas
        comparaciones_result = await self.db.execute(
            select(ComparacionReal).where(ComparacionReal.resultado_id == ultimo.id)
        )
        comparaciones = comparaciones_result.scalars().all()
        
        # Agrupar comparaciones por tipo
        comp_por_tipo = {
            'general': None,
            'por_periodo': [],
            'por_bloque': []
        }
        
        for comp in comparaciones:
            if comp.tipo_comparacion == 'general':
                comp_por_tipo['general'] = {
                    'modelo': float(comp.valor_modelo),
                    'real': float(comp.valor_real),
                    'accuracy': float(comp.accuracy),
                    'diferencia': float(comp.diferencia_absoluta),
                    'diferencia_pct': float(comp.diferencia_porcentual)
                }
            elif comp.tipo_comparacion == 'por_periodo':
                comp_por_tipo['por_periodo'].append({
                    'periodo': int(comp.dimension),
                    'modelo': float(comp.valor_modelo),
                    'real': float(comp.valor_real),
                    'accuracy': float(comp.accuracy)
                })
            elif comp.tipo_comparacion == 'por_bloque':
                comp_por_tipo['por_bloque'].append({
                    'bloque': comp.dimension,
                    'modelo': float(comp.valor_modelo),
                    'real': float(comp.valor_real),
                    'accuracy': float(comp.accuracy)
                })
        
        # Ordenar
        comp_por_tipo['por_periodo'].sort(key=lambda x: x['periodo'])
        comp_por_tipo['por_bloque'].sort(key=lambda x: x['bloque'])
        
        # Tendencia histórica de accuracy
        tendencia_accuracy = []
        for res in resultados[:10]:  # Últimos 10 turnos
            if res.accuracy_global:
                tendencia_accuracy.append({
                    'turno': res.turno,
                    'fecha': res.fecha_inicio.isoformat(),
                    'accuracy': float(res.accuracy_global)
                })
        
        # KPIs del modelo
        kpi_result = await self.db.execute(
            select(KPITurno).where(KPITurno.resultado_id == ultimo.id)
        )
        kpi = kpi_result.scalar_one_or_none()
        
        return {
            "turno_actual": {
                "turno": ultimo.turno,
                "fecha": ultimo.fecha_inicio.isoformat(),
                "codigo": ultimo.codigo,
                "accuracy_global": float(ultimo.accuracy_global),
                "modelo_movimientos": ultimo.total_movimientos_modelo,
                "real_movimientos": ultimo.total_movimientos_real,
                "brecha": ultimo.brecha_movimientos
            },
            "comparaciones": comp_por_tipo,
            "tendencia_accuracy": tendencia_accuracy,
            "metricas_optimizacion": {
                "bloques_optimizados": kpi.bloques_optimizados if kpi else 0,
                "ahorro_tiempo_min": float(kpi.ahorro_tiempo_minutos) if kpi and kpi.ahorro_tiempo_minutos else 0,
                "desviacion_promedio": float(kpi.desviacion_promedio_pct) if kpi and kpi.desviacion_promedio_pct else 0
            },
            "analisis": await self._analizar_precision_modelo(ultimo, comparaciones)
        }
    
    async def get_dashboard_cuotas_camiones(
        self,
        anio: Optional[int] = None,
        semana: Optional[int] = None,
        dia: Optional[int] = None,
        turno: Optional[int] = None
    ) -> Dict[str, Any]:
        """Obtiene datos para el dashboard de cuotas de camiones"""
        
        # Query base
        query = select(ResultadoCamila).where(
            ResultadoCamila.estado == EstadoProcesamiento.COMPLETADO
        )
        
        # Aplicar filtros
        if anio:
            query = query.where(ResultadoCamila.anio == anio)
        if semana:
            query = query.where(ResultadoCamila.semana == semana)
        if dia:
            query = query.where(ResultadoCamila.dia == dia)
        if turno:
            query = query.where(ResultadoCamila.turno == turno)
        
        result = await self.db.execute(query.order_by(ResultadoCamila.fecha_inicio.desc()).limit(1))
        resultado = result.scalar_