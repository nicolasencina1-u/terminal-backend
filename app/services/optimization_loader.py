# app/services/optimization_loader.py
import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, and_, func, update
from sqlalchemy.sql import text
import logging
from uuid import UUID
import re
import json
from pathlib import Path

from app.models.optimization import *

logger = logging.getLogger(__name__)

class OptimizationLoader:
    """Servicio para cargar datos del modelo de optimización"""
    
    def __init__(self, db: AsyncSession):
        self.db = db
        self.validation_errors = []
        self.warnings = []
        self._distancias_cache = {}
        self.distancias_modelo_filepath = None
        
    async def load_optimization_results(
        self,
        resultado_filepath: str,
        instancia_filepath: Optional[str],
        flujos_filepath: Optional[str],
        distancias_filepath: Optional[str],
        fecha_inicio: datetime,
        semana: int,
        anio: int,
        participacion: int,
        con_dispersion: bool,
        variant: str = "magdalena",
        criterio: Optional[int] = None,
        granularidad: Optional[str] = None
    ) -> UUID:
        """Carga completa de resultados de optimización"""
        
        # Guardar referencia al archivo de distancias del modelo
        self.distancias_modelo_filepath = distancias_filepath
        
        logger.info(f"{'='*80}")
        logger.info(f"Iniciando carga de optimización")
        logger.info(f"Resultado: {resultado_filepath}")
        logger.info(f"Config: Año {anio}, Semana {semana}, P{participacion}, Disp={'K' if con_dispersion else 'N'}")
        logger.info(f"Variante: Modelo {variant}, Criterio: {criterio if criterio else 'Sin criterio'}, Granularidad: {granularidad if granularidad else 'Sin granularidad'}")
        
        try:
            # 1. Crear o obtener escenario común
            escenario = await self._get_or_create_escenario(anio, semana, participacion, con_dispersion)
            
            # 2. Crear o actualizar instancia específica vinculada al escenario
            instancia = await self._create_or_update_instancia(
                escenario.id, fecha_inicio, semana, anio, participacion, con_dispersion, variant, criterio, granularidad
            )
            
            # Cargar bloques y segregaciones base si no existen
            await self._ensure_base_data()
            
            # Cargar archivo de resultado
            stats_resultado = await self._load_resultado_file(resultado_filepath, instancia.id)
            
            # Cargar archivo de instancia si existe
            stats_instancia = {}
            if instancia_filepath and Path(instancia_filepath).exists():
                stats_instancia = await self._load_instancia_file(instancia_filepath, instancia.id)
            
            # Cargar flujos reales si existen
            stats_flujos = {}
            if flujos_filepath and Path(flujos_filepath).exists():
                stats_flujos = await self._load_flujos_file(flujos_filepath, escenario.id, fecha_inicio)
            
            # Cargar distancias si existen
            if distancias_filepath and Path(distancias_filepath).exists():
                await self._load_distancias_file(distancias_filepath)
            
            # Calcular KPIs comparativos
            kpis_stats = await self._calculate_kpis(instancia.id)
            
            # Calcular métricas temporales
            await self._calculate_temporal_metrics(instancia.id)
            
            # Actualizar resultados generales
            await self._update_resultados_generales(
                instancia.id, stats_resultado, stats_flujos, kpis_stats
            )
            
            # Registrar log de procesamiento
            await self._log_procesamiento(
                instancia.id, 
                resultado_filepath, 
                'resultado',
                stats_resultado.get('total_registros', 0),
                'completado'
            )
            
            # Commit final
            await self.db.commit()
            
            # Log resumen
            self._log_summary(instancia.id, stats_resultado, stats_flujos, kpis_stats)
            
            return instancia.id
            
        except Exception as e:
            await self.db.rollback()
            logger.error(f"❌ Error cargando optimización: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            raise

    async def _get_or_create_escenario(self, anio: int, semana: int, participacion: int, con_dispersion: bool) -> Escenario:
        """Obtiene o crea un escenario para agrupar datos comunes"""
        query = select(Escenario).where(
            and_(
                Escenario.anio == anio,
                Escenario.semana == semana,
                Escenario.participacion == participacion,
                Escenario.con_dispersion == con_dispersion
            )
        )
        result = await self.db.execute(query)
        escenario = result.scalar_one_or_none()
        
        if not escenario:
            escenario = Escenario(
                anio=anio,
                semana=semana,
                participacion=participacion,
                con_dispersion=con_dispersion
            )
            self.db.add(escenario)
            await self.db.flush()
            logger.info(f"✨ Escenario creado: {anio}-W{semana}-P{participacion}")
        else:
            logger.info(f"✅ Escenario existente: {anio}-W{semana}-P{participacion}")
            
        return escenario

    async def _debug_distancias_movimientos(self, instancia_id: UUID):
        """Método temporal para debuggear por qué no se encuentran distancias"""
        
        logger.info("\n=== DEBUGGING DE DISTANCIAS ===")
        
        # 1. Verificar qué distancias hay en la base de datos
        dist_result = await self.db.execute(
            select(DistanciaReal).limit(20)
        )
        distancias_muestra = dist_result.scalars().all()
        
        logger.info(f"\nMuestra de distancias en BD:")
        for d in distancias_muestra[:10]:
            logger.info(f"  {d.origen} → {d.destino}: {d.distancia_metros}m")
        
        # 2. Verificar qué movimientos reales hay
        movs_result = await self.db.execute(
            select(MovimientoReal)
            .where(MovimientoReal.instancia_id == instancia_id)
            .limit(20)
        )
        movimientos_muestra = movs_result.scalars().all()
        
        logger.info(f"\nMuestra de movimientos reales:")
        for m in movimientos_muestra[:10]:
            logger.info(f"  {m.tipo_movimiento}: {m.bloque_origen} → {m.bloque_destino}")
        
        # 3. Intentar encontrar coincidencias manualmente
        logger.info(f"\nIntentando encontrar coincidencias:")
        for m in movimientos_muestra[:5]:
            if m.bloque_origen and m.bloque_destino:
                origen_norm = self._normalizar_ubicacion(m.bloque_origen)
                destino_norm = self._normalizar_ubicacion(m.bloque_destino)
                
                # Buscar distancia directa
                dist_result = await self.db.execute(
                    select(DistanciaReal).where(
                        and_(
                            DistanciaReal.origen == origen_norm,
                            DistanciaReal.destino == destino_norm
                        )
                    )
                )
                dist_directa = dist_result.scalar_one_or_none()
                
                # Buscar distancia inversa
                dist_result_inv = await self.db.execute(
                    select(DistanciaReal).where(
                        and_(
                            DistanciaReal.origen == destino_norm,
                            DistanciaReal.destino == origen_norm
                        )
                    )
                )
                dist_inversa = dist_result_inv.scalar_one_or_none()
                
                logger.info(f"\n  Movimiento: {m.bloque_origen} → {m.bloque_destino}")
                logger.info(f"  Normalizado: {origen_norm} → {destino_norm}")
                logger.info(f"  Distancia directa: {dist_directa.distancia_metros if dist_directa else 'NO ENCONTRADA'}")
                logger.info(f"  Distancia inversa: {dist_inversa.distancia_metros if dist_inversa else 'NO ENCONTRADA'}")
        
        # 4. Verificar conteo total de distancias
        count_result = await self.db.execute(
            select(func.count(DistanciaReal.id))
        )
        total_distancias = count_result.scalar()
        
        logger.info(f"\nTotal de distancias en BD: {total_distancias}")
        logger.info("=== FIN DEBUGGING ===\n")   
        
    async def _calculate_kpis(self, instancia_id: UUID) -> Dict[str, Any]:
        """Calcula KPIs comparativos usando el mapeo de Escenario"""
        
        logger.info("Calculando KPIs comparativos...")
        
        try:
            instancia = await self.db.get(Instancia, instancia_id)
            escenario_id = instancia.escenario_id

            # 1. Contar movimientos reales vinculados al escenario
            movs_real_result = await self.db.execute(
                select(
                    MovimientoReal.tipo_movimiento,
                    func.count(MovimientoReal.id).label('cantidad')
                ).where(
                    MovimientoReal.escenario_id == escenario_id
                ).group_by(MovimientoReal.tipo_movimiento)
            )
            
            movs_real_dict = {row.tipo_movimiento.value: row.cantidad for row in movs_real_result}
            
            real_stats = {
                'total': sum(movs_real_dict.values()),
                'yard': movs_real_dict.get('YARD', 0),
                'dlvr': movs_real_dict.get('DLVR', 0),
                'load': movs_real_dict.get('LOAD', 0),
                'recv': movs_real_dict.get('RECV', 0),
                'dsch': movs_real_dict.get('DSCH', 0)
            }
            
            if real_stats['total'] > 0:
                logger.info(f"Movimientos reales:")
                logger.info(f"  - Total: {real_stats['total']}")
                logger.info(f"  - YARD: {real_stats['yard']}")
                logger.info(f"  - DLVR: {real_stats['dlvr']}")
                logger.info(f"  - LOAD: {real_stats['load']}")
            else:
                logger.warning("No se encontraron movimientos reales")
        
            # 2. Calcular cobertura
            total_movs_query = await self.db.execute(
                select(func.count(MovimientoReal.id)).where(MovimientoReal.escenario_id == escenario_id)
            )
            total_sin_filtro = total_movs_query.scalar() or 0
            
            mapeados_query = await self.db.execute(
                select(func.count(MovimientoReal.id)).where(
                    and_(MovimientoReal.escenario_id == escenario_id, MovimientoReal.mapeado == True)
                )
            )
            total_mapeados = mapeados_query.scalar() or 0
            
            cobertura = (total_mapeados / total_sin_filtro * 100) if total_sin_filtro > 0 else 0
            logger.info(f"Movimientos totales en escenario: {total_sin_filtro}")
            logger.info(f"Cobertura del mapeo: {cobertura:.1f}%")
            
            # 3. Leer distancias del modelo desde archivo
            distancia_modelo_total = 0
            movimientos_dlvr_modelo = 0
            movimientos_load_modelo = 0
            
            if self.distancias_modelo_filepath and Path(self.distancias_modelo_filepath).exists():
                xl = pd.ExcelFile(self.distancias_modelo_filepath)
                if 'Resumen Semanal' in xl.sheet_names:
                    df_resumen = pd.read_excel(xl, 'Resumen Semanal')
                    df_resumen.columns = df_resumen.columns.str.strip()
                    if not df_resumen.empty:
                        row = df_resumen.iloc[0]
                        distancia_modelo_total = int(row.get('Distancia Total', 0))
                        movimientos_dlvr_modelo = int(row.get('Movimientos_DLVR', 0))
                        movimientos_load_modelo = int(row.get('Movimientos_LOAD', 0))

            # 4. Calcular distancias reales
            dist_result = await self.db.execute(select(DistanciaReal))
            mapa_distancias = {f"{d.origen}_{d.destino}": d.distancia_metros for d in dist_result.scalars().all()}
            
            movs_result = await self.db.execute(select(MovimientoReal).where(MovimientoReal.escenario_id == escenario_id))
            distancias_por_tipo = {'YARD': 0, 'DLVR': 0, 'RECV': 0, 'LOAD': 0, 'DSCH': 0, 'SHFT': 0, 'OTHR': 0}
            distancia_total_real = 0
            
            for mov in movs_result.scalars():
                if mov.bloque_origen and mov.bloque_destino:
                    origen, destino = self._normalizar_ubicacion(mov.bloque_origen), self._normalizar_ubicacion(mov.bloque_destino)
                    dist = mapa_distancias.get(f"{origen}_{destino}", 0) or mapa_distancias.get(f"{destino}_{origen}", 0)
                    if dist > 0:
                        distancia_total_real += dist
                        if mov.tipo_movimiento.value in distancias_por_tipo:
                            distancias_por_tipo[mov.tipo_movimiento.value] += dist
            
            # 5. Calcular métricas finales
            movimientos_operativos_real = real_stats['yard'] + real_stats['dlvr'] + real_stats['load']
            movimientos_operativos_modelo = movimientos_dlvr_modelo + movimientos_load_modelo or (real_stats['dlvr'] + real_stats['load'])
            
            reduccion_movs = movimientos_operativos_real - movimientos_operativos_modelo
            pct_reduccion_movs = (reduccion_movs / movimientos_operativos_real * 100) if movimientos_operativos_real > 0 else 0
            pct_reduccion_movs = max(-999.99, min(999.99, float(pct_reduccion_movs)))
            
            if distancia_modelo_total == 0:
                distancia_modelo_total = distancia_total_real - distancias_por_tipo['YARD']
            
            dist_ahorrada = distancia_total_real - distancia_modelo_total
            eficiencia = (dist_ahorrada / distancia_total_real * 100) if distancia_total_real > 0 else 0
            eficiencia = max(-999.99, min(999.99, float(eficiencia)))
            
            # 6. Guardar KPIs
            kpis_list = [
                {'metrica': 'movimientos_operativos_total', 'valor_real': movimientos_operativos_real, 'valor_modelo': movimientos_operativos_modelo, 'diferencia': reduccion_movs, 'porcentaje_mejora': pct_reduccion_movs, 'unidad': 'movimientos'},
                {'metrica': 'distancia_total', 'valor_real': distancia_total_real, 'valor_modelo': distancia_modelo_total, 'diferencia': dist_ahorrada, 'porcentaje_mejora': eficiencia, 'unidad': 'metros'}
            ]
            for data in kpis_list:
                self.db.add(KPIComparativo(instancia_id=instancia_id, categoria='general', **data))
            
            # Ocupación
            ocup_result = await self.db.execute(select(func.avg(OcupacionBloque.porcentaje_ocupacion)).where(OcupacionBloque.instancia_id == instancia_id))
            
            # Contar total de segregaciones
            total_segs_result = await self.db.execute(select(func.count(Segregacion.id)))
            total_segregaciones_sistema = total_segs_result.scalar() or 0

            # Contar segregaciones del modelo optimizado
            segregaciones_opt_result = await self.db.execute(
                select(func.count(func.distinct(MovimientoModelo.segregacion_id)))
                .where(MovimientoModelo.instancia_id == instancia_id)
            )
            segregaciones_optimizadas = segregaciones_opt_result.scalar() or 0

            await self.db.flush()
            return {
                'movimientos_real': real_stats['total'], 
                'movimientos_yard': real_stats['yard'],
                'movimientos_dlvr_real': real_stats['dlvr'], 
                'movimientos_load_real': real_stats['load'],
                'movimientos_recv_real': real_stats['recv'], 
                'movimientos_dsch_real': real_stats['dsch'],
                'movimientos_modelo': movimientos_operativos_modelo, 
                'movimientos_reduccion': reduccion_movs,
                'movimientos_reduccion_pct': pct_reduccion_movs, 
                'distancia_total_real': distancia_total_real,
                'distancia_total_modelo': distancia_modelo_total, 
                'distancia_ahorrada': dist_ahorrada,
                'eficiencia_ganada': eficiencia, 
                'ocupacion_promedio': float(ocup_result.scalar() or 0),
                'total_segregaciones_sistema': total_segregaciones_sistema,
                'segregaciones_optimizadas': segregaciones_optimizadas,
                'cobertura_optimizacion': cobertura
            }
        except Exception as e:
            logger.error(f"Error calculando KPIs: {e}")
            raise

    async def _update_resultados_generales(
        self, instancia_id: UUID, stats_resultado: Dict,
        stats_flujos: Dict, kpis: Dict
    ):
        """Actualiza tabla de resultados generales con distancias - VERSIÓN MEJORADA"""
        
        logger.info("Actualizando resultados generales con distancias...")
        
        # Obtener totales de segregaciones
        segs_result = await self.db.execute(
            select(func.count(func.distinct(Segregacion.id)))
        )
        total_segregaciones = segs_result.scalar() or 0
        
        # Obtener carga de trabajo total
        carga_result = await self.db.execute(
            select(
                func.sum(CargaTrabajo.carga_trabajo).label('total'),
                func.max(CargaTrabajo.carga_trabajo).label('maxima'),
                func.min(CargaTrabajo.carga_trabajo).label('minima')
            )
            .where(CargaTrabajo.instancia_id == instancia_id)
        )
        carga_stats = carga_result.one()
        
        # Calcular capacidad total actualizada
        capacidad_result = await self.db.execute(
            select(func.sum(Bloque.capacidad_teus))
        )
        capacidad_total = capacidad_result.scalar() or 0
        
        resultado = ResultadoGeneral(
            instancia_id=instancia_id,
            # Movimientos
            movimientos_reales_total=stats_flujos.get('total_movimientos', 0),
            movimientos_yard_real=kpis.get('movimientos_yard', 0),
            movimientos_dlvr_real=kpis.get('movimientos_dlvr_real', 0),
            movimientos_load_real=kpis.get('movimientos_load_real', 0),
            movimientos_recv_real=kpis.get('movimientos_recv_real', 0),
            movimientos_dsch_real=kpis.get('movimientos_dsch_real', 0),
            movimientos_optimizados=kpis.get('movimientos_modelo', 0),
            movimientos_dlvr_modelo=kpis.get('movimientos_dlvr_modelo', 0),
            movimientos_load_modelo=kpis.get('movimientos_load_modelo', 0),
            movimientos_reduccion=kpis.get('reduccion_movimientos', 0),
            movimientos_reduccion_pct=kpis.get('porcentaje_reduccion_movimientos', 0),
            
            # Distancias
            distancia_real_total=kpis.get('distancia_total_real', 0),
            distancia_real_load=kpis.get('distancia_load_real', 0),
            distancia_real_dlvr=kpis.get('distancia_dlvr_real', 0),
            distancia_real_yard=kpis.get('distancia_yard', 0),
            distancia_modelo_total=kpis.get('distancia_total_modelo', 0),
            distancia_modelo_load=kpis.get('distancia_load_modelo', 0),
            distancia_modelo_dlvr=kpis.get('distancia_dlvr_modelo', 0),
            distancia_reduccion=kpis.get('distancia_ahorrada', 0),
            distancia_reduccion_pct=kpis.get('eficiencia_ganada', 0),
            
            # Eficiencia
            eficiencia_real=100 - (kpis.get('movimientos_yard', 0) / stats_flujos.get('total_movimientos', 1) * 100) if stats_flujos.get('total_movimientos', 0) > 0 else 0,
            eficiencia_modelo=100,  # Sin YARD
            eficiencia_ganancia=kpis.get('eficiencia_ganada', 0),
            
            # Segregaciones
            segregaciones_total=total_segregaciones,
            segregaciones_optimizadas=total_segregaciones,
            
            # Carga de trabajo
            carga_trabajo_total=carga_stats.total or 0,
            carga_maxima=carga_stats.maxima or 0,
            carga_minima=carga_stats.minima or 0,
            variacion_carga=stats_resultado.get('variacion_carga', 0),
            balance_carga=stats_resultado.get('balance_carga', 0),
            
            # Ocupación
            ocupacion_promedio_pct=kpis.get('ocupacion_promedio', 0),
            ocupacion_maxima_pct=kpis.get('ocupacion_maxima', 0),
            ocupacion_minima_pct=kpis.get('ocupacion_minima', 0),
            capacidad_total_teus=capacidad_total,
            
            # Metadata
            archivo_distancias_usado=Path(self.distancias_modelo_filepath).name if self.distancias_modelo_filepath else None
        )
        
        self.db.add(resultado)
        await self.db.flush()
        
        # Actualizar estado de instancia
        instancia_result = await self.db.execute(
            select(Instancia).where(Instancia.id == instancia_id)
        )
        instancia = instancia_result.scalar_one()
        instancia.estado = 'completado'
        instancia.total_movimientos = kpis.get('movimientos_modelo', 0)
        instancia.total_bloques = len(stats_resultado.get('bloques_activos', set()))
        instancia.total_segregaciones = kpis.get('total_segregaciones_sistema', 0)
        
        await self.db.flush()
        
        logger.info(f"Resultados actualizados:")
        logger.info(f"  - Movimientos optimizados: {kpis.get('movimientos_modelo', 0)}")
        logger.info(f"  - Reducción movimientos: {kpis.get('porcentaje_reduccion_movimientos', 0):.1f}%")
        logger.info(f"  - Distancia ahorrada: {kpis.get('distancia_ahorrada', 0):,} m")
        logger.info(f"  - Eficiencia ganada: {kpis.get('eficiencia_ganada', 0):.2f}%")

    async def _load_resultado_file(self, filepath: str, instancia_id: UUID) -> Dict[str, Any]:
        """Carga archivo de resultados del modelo - VERSIÓN MEJORADA CON CAPACIDADES"""
        
        logger.info("Cargando archivo de resultados...")
        
        try:
            xl = pd.ExcelFile(filepath)
            logger.info(f"Hojas disponibles: {xl.sheet_names}")
            
            stats = {
                'total_registros': 0,
                'movimientos_modelo': 0,
                'carga_trabajo': 0,
                'ocupacion': 0,
                'segregaciones': set(),
                'bloques_activos': set(),
                'variacion_carga': 0,
                'balance_carga': 0,
                'asignaciones_bloques': 0,
                'capacidades_actualizadas': False
            }
            
            # Obtener mapeo de bloques
            bloques_map = await self._get_bloques_map()
            
            # 0. Actualizar capacidades de bloques desde hoja Ocupación Bloques
            if 'Ocupación Bloques' in xl.sheet_names:
                df_ocupacion = pd.read_excel(xl, 'Ocupación Bloques')
                logger.info("Actualizando capacidades de bloques desde archivo...")
                
                # Obtener capacidades únicas por bloque
                capacidades_bloques = {}
                for idx, row in df_ocupacion.iterrows():
                    bloque_codigo = str(row.get('Bloque', '')).strip()
                    capacidad = row.get('Capacidad Bloque', 0)
                    
                    if bloque_codigo and capacidad > 0 and bloque_codigo not in capacidades_bloques:
                        capacidades_bloques[bloque_codigo] = int(capacidad)
                
                # Actualizar capacidades en base de datos
                for bloque_codigo, capacidad in capacidades_bloques.items():
                    if bloque_codigo in bloques_map:
                        await self.db.execute(
                            update(Bloque)
                            .where(Bloque.id == bloques_map[bloque_codigo])
                            .values(capacidad_teus=capacidad)
                        )
                        logger.info(f"  - {bloque_codigo}: {capacidad} TEUs")
                
                await self.db.flush()
                stats['capacidades_actualizadas'] = True
            
            # 1. Cargar hoja General (movimientos del modelo)
            if 'General' in xl.sheet_names:
                df_general = pd.read_excel(xl, 'General')
                logger.info(f"Procesando {len(df_general)} registros de General")
                
                batch = []
                for idx, row in df_general.iterrows():
                    try:
                        bloque_codigo = str(row.get('Bloque', '')).strip()
                        segregacion_codigo = str(row.get('Segregación', '')).strip()
                        
                        if bloque_codigo in bloques_map:
                            # Crear o obtener segregación
                            segregacion = await self._get_or_create_segregacion(segregacion_codigo)
                            
                            mov = MovimientoModelo(
                                instancia_id=instancia_id,
                                segregacion_id=segregacion.id,
                                bloque_id=bloques_map[bloque_codigo],
                                periodo=int(row.get('Periodo', 0)),
                                recepcion=int(row.get('Recepción', 0)),
                                carga=int(row.get('Carga', 0)),
                                descarga=int(row.get('Descarga', 0)),
                                entrega=int(row.get('Entrega', 0)),
                                volumen_teus=int(row.get('Volumen (TEUs)', 0)),
                                bahias_ocupadas=int(row.get('Bahías Ocupadas', 0))
                            )
                            batch.append(mov)
                            
                            total_mov = mov.recepcion + mov.carga + mov.descarga + mov.entrega
                            if total_mov > 0:
                                stats['movimientos_modelo'] += total_mov
                                stats['bloques_activos'].add(bloque_codigo)
                                stats['segregaciones'].add(segregacion_codigo)
                        
                        if len(batch) >= 100:
                            self.db.add_all(batch)
                            await self.db.flush()
                            batch = []
                            
                    except Exception as e:
                        logger.warning(f"Error en fila {idx} de General: {str(e)}")
                
                if batch:
                    self.db.add_all(batch)
                    await self.db.flush()
                
                stats['total_registros'] += len(df_general)
            
            # 2. Cargar Total bloques (asignaciones)
            if 'Total bloques' in xl.sheet_names:
                df_bloques = pd.read_excel(xl, 'Total bloques')
                logger.info(f"Procesando asignaciones de bloques")
                
                for idx, row in df_bloques.iterrows():
                    try:
                        segregacion_codigo = str(row.get('Segregación', '')).strip()
                        total_bloques = int(row.get('Total bloques asignadas', 0))
                        
                        if total_bloques > 0:
                            segregacion = await self._get_or_create_segregacion(segregacion_codigo)
                            
                            # Obtener bloques asignados a esta segregación de la hoja General
                            bloques_asignados = await self._get_bloques_asignados(
                                instancia_id, segregacion.id
                            )
                            
                            asignacion = AsignacionBloque(
                                instancia_id=instancia_id,
                                segregacion_id=segregacion.id,
                                total_bloques_asignados=total_bloques,
                                bloques_codigos=list(bloques_asignados)
                            )
                            self.db.add(asignacion)
                            stats['asignaciones_bloques'] += 1
                            
                    except Exception as e:
                        logger.warning(f"Error en fila {idx} de Total bloques: {str(e)}")
                
                await self.db.flush()
            
            # 3. Cargar Workload bloques
            if 'Workload bloques' in xl.sheet_names:
                df_workload = pd.read_excel(xl, 'Workload bloques')
                logger.info(f"Procesando {len(df_workload)} registros de Workload")
                
                batch = []
                cargas = []
                cargas_por_periodo = {}
                
                for idx, row in df_workload.iterrows():
                    try:
                        bloque_codigo = str(row.get('Bloque', '')).strip()
                        periodo = int(row.get('Periodo', 0))
                        
                        if bloque_codigo in bloques_map:
                            carga_valor = int(row.get('Carga de trabajo', 0))
                            carga = CargaTrabajo(
                                instancia_id=instancia_id,
                                bloque_id=bloques_map[bloque_codigo],
                                periodo=periodo,
                                carga_trabajo=carga_valor
                            )
                            batch.append(carga)
                            stats['carga_trabajo'] += carga_valor
                            cargas.append(carga_valor)
                            
                            # Agrupar por periodo para max/min
                            if periodo not in cargas_por_periodo:
                                cargas_por_periodo[periodo] = []
                            cargas_por_periodo[periodo].append(carga_valor)
                        
                        if len(batch) >= 100:
                            self.db.add_all(batch)
                            await self.db.flush()
                            batch = []
                            
                    except Exception as e:
                        logger.warning(f"Error en fila {idx} de Workload: {str(e)}")
                
                if batch:
                    self.db.add_all(batch)
                    await self.db.flush()
                
                # Calcular balance de carga (desviación estándar)
                if cargas:
                    stats['balance_carga'] = int(np.std(cargas))
            
            # 4. Cargar Carga máx-min si existe
            if 'Carga máx-min' in xl.sheet_names:
                df_carga_maxmin = pd.read_excel(xl, 'Carga máx-min')
                logger.info("Procesando cargas máximas y mínimas por periodo")
                
                for idx, row in df_carga_maxmin.iterrows():
                    try:
                        periodo = int(row.get('Periodo', 0))
                        carga_max = int(row.get('Carga máxima', 0))
                        carga_min = int(row.get('Carga mínima', 0))
                        
                        # Actualizar registros de carga trabajo con max/min
                        await self.db.execute(
                            update(CargaTrabajo)
                            .where(and_(
                                CargaTrabajo.instancia_id == instancia_id,
                                CargaTrabajo.periodo == periodo
                            ))
                            .values(
                                carga_maxima=carga_max,
                                carga_minima=carga_min
                            )
                        )
                    except Exception as e:
                        logger.warning(f"Error en fila {idx} de Carga máx-min: {str(e)}")
                
                await self.db.flush()
            
            # 5. Cargar Contenedores Turno-Bloque (ocupación)
            if 'Contenedores Turno-Bloque' in xl.sheet_names:
                df_contenedores = pd.read_excel(xl, 'Contenedores Turno-Bloque')
                logger.info(f"Procesando ocupación por turno-bloque")
                
                batch = []
                columnas_bloques = [col for col in df_contenedores.columns if col != 'Turno' and col in bloques_map]
                
                for idx, row in df_contenedores.iterrows():
                    try:
                        turno = int(row.get('Turno', 0))
                        periodo = turno
                        
                        for bloque_codigo in columnas_bloques:
                            contenedores = int(row.get(bloque_codigo, 0))
                            
                            # Obtener capacidad actualizada del bloque
                            bloque_result = await self.db.execute(
                                select(Bloque).where(Bloque.codigo == bloque_codigo)
                            )
                            bloque = bloque_result.scalar_one()
                            
                            porcentaje = (contenedores / bloque.capacidad_teus * 100) if bloque.capacidad_teus > 0 else 0
                            
                            ocupacion = OcupacionBloque(
                                instancia_id=instancia_id,
                                bloque_id=bloques_map[bloque_codigo],
                                periodo=periodo,
                                turno=((periodo - 1) % 3) + 1,
                                contenedores_teus=contenedores,
                                capacidad_bloque=bloque.capacidad_teus,
                                porcentaje_ocupacion=porcentaje,
                                estado='activo' if contenedores > 0 else 'inactivo'
                            )
                            batch.append(ocupacion)
                            stats['ocupacion'] += 1
                        
                        if len(batch) >= 100:
                            self.db.add_all(batch)
                            await self.db.flush()
                            batch = []
                            
                    except Exception as e:
                        logger.warning(f"Error en fila {idx} de Contenedores: {str(e)}")
                
                if batch:
                    self.db.add_all(batch)
                    await self.db.flush()
            
            # 6. Procesar hoja de Variación Carga de trabajo
            if 'Variación Carga de trabajo' in xl.sheet_names:
                try:
                    df_var = pd.read_excel(xl, 'Variación Carga de trabajo')
                    logger.info(f"Procesando hoja Variación Carga de trabajo")
                    
                    variacion_valor = None
                    
                    if len(df_var) > 0 and len(df_var.columns) > 0:
                        if len(df_var) > 1:
                            primer_valor = df_var.iloc[0, 0]
                            if isinstance(primer_valor, str) and 'variación' in primer_valor.lower():
                                variacion_valor = df_var.iloc[1, 0]
                            else:
                                variacion_valor = primer_valor
                        else:
                            variacion_valor = df_var.iloc[0, 0]
                    
                    if variacion_valor is not None and pd.notna(variacion_valor):
                        try:
                            stats['variacion_carga'] = int(float(str(variacion_valor)))
                            logger.info(f"✓ Variación de carga: {stats['variacion_carga']}")
                        except (ValueError, TypeError) as e:
                            logger.warning(f"No se pudo convertir variación de carga a entero: {variacion_valor}")
                            stats['variacion_carga'] = 0
                    else:
                        logger.warning("No se encontró valor de variación de carga")
                        stats['variacion_carga'] = 0
                        
                except Exception as e:
                    logger.warning(f"Error procesando hoja Variación Carga de trabajo: {str(e)}")
                    stats['variacion_carga'] = 0
            
            await self.db.flush()
            
            logger.info(f"Resultado cargado: {stats}")
            logger.info(f"  - Capacidades actualizadas: {'Sí' if stats['capacidades_actualizadas'] else 'No'}")
            logger.info(f"  - Asignaciones de bloques: {stats['asignaciones_bloques']}")
            
            return stats
            
        except Exception as e:
            logger.error(f"Error cargando resultado: {e}")
            raise

    async def _get_bloques_asignados(self, instancia_id: UUID, segregacion_id: int) -> set:
        """Obtiene los bloques asignados a una segregación"""
        result = await self.db.execute(
            select(func.distinct(Bloque.codigo))
            .join(MovimientoModelo, MovimientoModelo.bloque_id == Bloque.id)
            .where(and_(
                MovimientoModelo.instancia_id == instancia_id,
                MovimientoModelo.segregacion_id == segregacion_id
            ))
        )
        return set([r[0] for r in result.all()])

    def _normalizar_ubicacion(self, ubicacion: str) -> str:
        """Normaliza códigos de ubicación para coincidencia en distancias"""
        ubicacion = str(ubicacion).strip().upper()
        
        # Mapeos comunes
        mapeos = {
            'GATE': 'GATE',
            'PUERTA': 'GATE',
            'SITIO1': 'SITIO_SUR',
            'SITIO 1': 'SITIO_SUR',
            'SITIO_1': 'SITIO_SUR',
            'SUR': 'SITIO_SUR',
            'SITIO2': 'SITIO_NORTE',
            'SITIO 2': 'SITIO_NORTE',
            'SITIO_2': 'SITIO_NORTE',
            'NORTE': 'SITIO_NORTE'
        }
        
        # Verificar mapeos
        for key, value in mapeos.items():
            if key in ubicacion:
                return value
        
        return ubicacion
    
    async def _create_or_update_instancia(self, escenario_id: UUID, fecha_inicio: datetime, semana: int, anio: int,
                                         participacion: int, con_dispersion: bool,
                                         variant: str, criterio: int, granularidad: str) -> Instancia:
        """Crea o actualiza una instancia vinculada a un escenario"""
        
        # Calcular fecha fin (7 días después)
        fecha_fin = fecha_inicio + timedelta(days=6)
        
        # Generar código único
        fecha_str = fecha_inicio.strftime('%Y%m%d')
        dispersion_str = 'K' if con_dispersion else 'N'
        codigo = f"{fecha_str}_{participacion}_{dispersion_str}_{variant}"
        if criterio: codigo += f"_c{criterio}"
        if granularidad: codigo += f"_{granularidad}"
        
        # Buscar instancia existente
        query = select(Instancia).where(Instancia.codigo == codigo)
        result = await self.db.execute(query)
        instancia = result.scalar_one_or_none()
        
        if instancia:
            logger.info(f"Actualizando instancia existente: {instancia.id}")
            # Limpiar datos anteriores
            await self._delete_instancia_data(instancia.id)
            instancia.escenario_id = escenario_id
            instancia.fecha_procesamiento = datetime.utcnow()
        else:
            logger.info("Creando nueva instancia")
            instancia = Instancia(
                codigo=codigo,
                escenario_id=escenario_id,
                fecha_inicio=fecha_inicio,
                fecha_fin=fecha_fin,
                anio=anio,
                semana=semana,
                escenario_nombre=f"Participación {participacion}%",
                participacion=participacion,
                con_dispersion=con_dispersion,
                variant=variant,
                criterio=criterio,
                granularidad=granularidad,
                periodos=21,
                dias=7,
                turnos_por_dia=3,
                estado='procesando',
                fecha_procesamiento=datetime.utcnow()
            )
            self.db.add(instancia)
            await self.db.flush()
        
        logger.info(f"Instancia ID: {instancia.id}, Código: {codigo}")
        return instancia
    
    async def _delete_instancia_data(self, instancia_id: UUID):
        """Elimina datos anteriores de una instancia"""
        logger.info(f"Eliminando datos anteriores de instancia {instancia_id}")
        
        await self.db.execute(delete(MovimientoModelo).where(MovimientoModelo.instancia_id == instancia_id))
        await self.db.execute(delete(OcupacionBloque).where(OcupacionBloque.instancia_id == instancia_id))
        await self.db.execute(delete(CargaTrabajo).where(CargaTrabajo.instancia_id == instancia_id))
        await self.db.execute(delete(KPIComparativo).where(KPIComparativo.instancia_id == instancia_id))
        await self.db.execute(delete(MetricaTemporal).where(MetricaTemporal.instancia_id == instancia_id))
        await self.db.execute(delete(ResultadoGeneral).where(ResultadoGeneral.instancia_id == instancia_id))
        await self.db.execute(delete(AsignacionBloque).where(AsignacionBloque.instancia_id == instancia_id))
        await self.db.flush()
    
    async def _ensure_base_data(self):
        """Asegura que existan los datos base de bloques"""
        
        # Verificar si ya existen bloques
        result = await self.db.execute(select(func.count(Bloque.id)))
        count = result.scalar()
        
        if count == 0:
            logger.info("Creando bloques base...")
            
            # Capacidades por defecto basadas en los datos
            capacidades = {
                'C1': 1155, 'C2': 1225, 'C3': 1400, 'C4': 1400,
                'C5': 490, 'C6': 1015, 'C7': 1015, 'C8': 980, 'C9': 420
            }
            
            for codigo, capacidad in capacidades.items():
                bloque = Bloque(
                    codigo=codigo,
                    capacidad_teus=capacidad,
                    capacidad_bahias=35,  # Por defecto
                    capacidad_original=capacidad  # Guardar original
                )
                self.db.add(bloque)
            
            await self.db.flush()
            logger.info(f"✓ Creados {len(capacidades)} bloques")
    
    async def _load_instancia_file(self, filepath: str, instancia_id: UUID) -> Dict[str, Any]:
        """Carga archivo de instancia con mapeo de segregaciones desde hoja KI_s"""
        
        logger.info("Cargando archivo de instancia con mapeo...")
        
        try:
            xl = pd.ExcelFile(filepath)
            stats = {
                'parametros': 0, 
                'segregaciones_info': 0,
                'mapeos_creados': 0,
                'segregaciones_mapeadas': set()
            }
            
            # 1. Cargar mapeo de segregaciones desde hoja KI_s
            if 'KI_s' in xl.sheet_names:
                logger.info("Procesando mapeo de segregaciones desde hoja KI_s...")
                df_mapeo = pd.read_excel(xl, 'KI_s')
                df_mapeo.columns = [str(col).strip().lower() for col in df_mapeo.columns]
                
                # Diccionario para almacenar el mapeo
                mapeo_segregaciones = {}
                
                for idx, row in df_mapeo.iterrows():
                    try:
                        # Columna S tiene el código de segregación (S1, S2, etc.)
                        codigo_s = str(row.get('s', '')).strip()
                        # Columna Segregacion tiene la descripción completa
                        segregacion_completa = str(row.get('segregacion', '')).strip()
                        
                        if codigo_s and segregacion_completa:
                            # Parsear la segregación completa
                            # Formato esperado: tipo-categoria-tamano-codigo (ej: expo-dry-40-EU238)
                            partes = segregacion_completa.split('-')
                            
                            tipo = 'desconocido'
                            categoria = 'desconocido'
                            tamano = None
                            codigo_real = codigo_s  # Por defecto usar el código S
                            
                            if len(partes) >= 4:
                                tipo = partes[0]  # expo/impo
                                categoria = partes[1]  # dry/reefer/empty
                                try:
                                    tamano = int(partes[2])  # 20/40
                                except:
                                    tamano = None
                                codigo_real = partes[3]  # EU238, MK566, etc.
                            
                            # Crear o actualizar segregación en la BD
                            segregacion = await self._get_or_create_segregacion(
                                codigo=codigo_s,
                                descripcion=segregacion_completa,
                                tipo=tipo,
                                categoria=categoria,
                                tamano=tamano
                            )
                            
                            # Guardar mapeo para uso posterior
                            mapeo_segregaciones[codigo_real] = codigo_s
                            mapeo_segregaciones[segregacion_completa] = codigo_s
                            # También mapear variaciones comunes
                            mapeo_segregaciones[f"{codigo_real}-{tipo}"] = codigo_s
                            mapeo_segregaciones[f"{codigo_real}-{tipo}-{categoria}"] = codigo_s
                            
                            stats['mapeos_creados'] += 1
                            stats['segregaciones_mapeadas'].add(codigo_s)
                            
                            logger.debug(f"Mapeado: {segregacion_completa} -> {codigo_s}")
                            
                    except Exception as e:
                        logger.warning(f"Error procesando fila {idx} de KI_s: {str(e)}")
                
                # Guardar el mapeo en el objeto para uso posterior
                self.mapeo_segregaciones = mapeo_segregaciones
                
                logger.info(f"✓ {stats['mapeos_creados']} mapeos de segregaciones creados")
                logger.info(f"✓ {len(stats['segregaciones_mapeadas'])} segregaciones únicas mapeadas")
            
            # 2. Cargar información adicional de segregaciones si existe
            if 'S' in xl.sheet_names:
                df_s = pd.read_excel(xl, 'S')
                for idx, row in df_s.iterrows():
                    if pd.notna(row.iloc[0]):
                        codigo = str(row.iloc[0]).strip()
                        descripcion = str(row.iloc[1]).strip() if len(row) > 1 and pd.notna(row.iloc[1]) else ''
                        
                        # Actualizar segregación con descripción adicional si no existe
                        segregacion = await self._get_or_create_segregacion(codigo, descripcion)
                        stats['segregaciones_info'] += 1
            
            logger.info(f"Instancia cargada: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"Error cargando instancia: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {
                'parametros': 0, 
                'segregaciones_info': 0,
                'mapeos_creados': 0,
                'segregaciones_mapeadas': set()
            }
    
    async def _load_flujos_file(self, filepath: str, escenario_id: UUID, fecha_inicio: datetime) -> Dict[str, Any]:
        """Carga archivo de flujos reales vinculados al escenario"""
        
        logger.info("Cargando archivo de flujos reales...")
        
        try:
            # 1. Verificar si ya existen flujos para este escenario
            count_query = await self.db.execute(
                select(func.count(MovimientoReal.id)).where(MovimientoReal.escenario_id == escenario_id)
            )
            if count_query.scalar() > 0:
                logger.info("✅ Ya existen movimientos reales para este escenario. Saltando carga.")
                # Obtener estadísticas básicas para el retorno
                stats_query = await self.db.execute(
                    select(
                        func.count(MovimientoReal.id).label('total'),
                        func.count(and_(MovimientoReal.tipo_movimiento == 'YARD')).label('yard')
                    ).where(MovimientoReal.escenario_id == escenario_id)
                )
                res = stats_query.one()
                return {'total_movimientos': res.total, 'yard': res.yard}

            df = pd.read_excel(filepath)
            df.columns = [str(col).strip().lower() for col in df.columns]
            
            logger.info(f"Procesando {len(df)} movimientos reales")
            
            stats = {
                'total_movimientos': 0,
                'yard': 0, 'dlvr': 0, 'recv': 0, 'load': 0, 'dsch': 0,
                'shft': 0, 'othr': 0, 'ignorados': 0,
                'movimientos_mapeados': 0, 'movimientos_sin_mapeo': 0
            }
            
            TIPOS_VALIDOS = ['RECV', 'DLVR', 'LOAD', 'DSCH', 'YARD']
            batch = []
            
            # Usar el mapeo si existe
            mapeo = getattr(self, 'mapeo_segregaciones', {})
            
            for idx, row in df.iterrows():
                try:
                    tipo_mov = str(row.get('ime_move_kind', '')).upper()
                    if tipo_mov not in TIPOS_VALIDOS:
                        stats['ignorados'] += 1
                        continue
                    
                    fecha_hora = pd.to_datetime(row.get('ime_time'))
                    dias_diff = (fecha_hora.date() - fecha_inicio.date()).days
                    hora = fecha_hora.hour
                    
                    turno = 1 if 8 <= hora < 16 else (2 if 16 <= hora < 24 else 3)
                    periodo = dias_diff * 3 + turno
                    
                    # Intentar buscar la columna de criterio (fallback secuencial)
                    segregacion_original = str(row.get('criterio_iii', row.get('criterio_ii', row.get('criterio_i', ''))))
                    segregacion_mapeada = None
                    segregacion_id = None
                    
                    if segregacion_original and mapeo:
                        for key, value in mapeo.items():
                            if key in segregacion_original or segregacion_original in key:
                                segregacion_mapeada = value
                                break
                        
                        if segregacion_mapeada:
                            seg_result = await self.db.execute(
                                select(Segregacion).where(Segregacion.codigo == segregacion_mapeada)
                            )
                            seg = seg_result.scalar_one_or_none()
                            if seg:
                                segregacion_id = seg.id
                                stats['movimientos_mapeados'] += 1
                    
                    mov = MovimientoReal(
                        escenario_id=escenario_id,
                        fecha_hora=fecha_hora,
                        bloque_origen=str(row.get('ime_fm', '')),
                        bloque_destino=str(row.get('ime_to', '')),
                        tipo_movimiento=tipo_mov,
                        segregacion=segregacion_original,
                        segregacion_mapeada=segregacion_mapeada,
                        segregacion_mapeada_id=segregacion_id,
                        mapeado=(segregacion_id is not None),
                        categoria=str(row.get('iu_category', '')),
                        contenedor_id=str(row.get('ime_ufv_gkey', '')),
                        turno=turno,
                        dia=dias_diff + 1,
                        periodo=periodo
                    )
                    batch.append(mov)
                    
                    stats['total_movimientos'] += 1
                    key_stats = tipo_mov.lower()
                    if key_stats in stats: stats[key_stats] += 1
                    
                    if len(batch) >= 500:
                        self.db.add_all(batch)
                        await self.db.flush()
                        batch = []
                        
                except Exception as e:
                    logger.warning(f"Error en fila {idx} de flujos: {str(e)}")
            
            if batch:
                self.db.add_all(batch)
                await self.db.flush()
            
            return stats
            
        except Exception as e:
            logger.error(f"Error cargando flujos: {e}")
            raise
    
    async def _load_distancias_file(self, filepath: str):
        """Carga archivo de distancias con TODAS las hojas - VERSIÓN CORREGIDA"""
        
        logger.info("Cargando archivo de distancias completo...")
        logger.info(f"Archivo: {filepath}")
        
        try:
            # Verificar si es archivo de Costanera o del modelo
            filename = Path(filepath).name
            es_costanera = 'Costanera' in filename
            
            xl = pd.ExcelFile(filepath)
            logger.info(f"Hojas de distancias disponibles: {xl.sheet_names}")
            logger.info(f"Es archivo Costanera: {'Sí' if es_costanera else 'No'}")
            
            distancias_cargadas = 0
            
            # Si es archivo Costanera, cargar TODAS las distancias
            if es_costanera:
                # 1. Cargar distancias entre bloques (hoja "Remanejo")
                if 'Remanejo' in xl.sheet_names:
                    df_remanejo = pd.read_excel(xl, 'Remanejo')
                    logger.info("Cargando distancias entre bloques desde hoja Remanejo...")
                    
                    # La primera columna tiene los bloques origen
                    for idx in range(len(df_remanejo)):
                        origen = str(df_remanejo.iloc[idx, 0]).strip()
                        if pd.isna(origen) or origen == 'fm/to' or not origen:
                            continue
                        
                        # Iterar sobre las columnas (bloques destino)
                        for col_idx in range(1, len(df_remanejo.columns)):
                            destino = str(df_remanejo.columns[col_idx]).strip()
                            if destino == 'fm/to' or not destino:
                                continue
                            
                            distancia = df_remanejo.iloc[idx, col_idx]
                            if pd.notna(distancia) and distancia > 0:
                                await self._insert_distancia(
                                    origen, destino, int(distancia), 'bloque', 'bloque'
                                )
                                distancias_cargadas += 1
                
                # 2. Cargar distancias bloque-gate (hoja "All")
                if 'All' in xl.sheet_names:
                    df_all = pd.read_excel(xl, 'All')
                    logger.info("Cargando distancias bloque-gate y bloque-sitio desde hoja All...")
                    
                    for idx, row in df_all.iterrows():
                        bloque = str(row.get('Bloque', '')).strip()
                        if not bloque or bloque == 'Bloque' or pd.isna(bloque):
                            continue
                        
                        # Gate
                        if 'Gate' in row and pd.notna(row['Gate']) and row['Gate'] > 0:
                            await self._insert_distancia(
                                bloque, 'GATE', int(row['Gate']), 'bloque', 'gate'
                            )
                            await self._insert_distancia(
                                'GATE', bloque, int(row['Gate']), 'gate', 'bloque'
                            )
                            distancias_cargadas += 2
                        
                        # Sitio Sur
                        if 'Sitio 1 - Sur' in row and pd.notna(row['Sitio 1 - Sur']) and row['Sitio 1 - Sur'] > 0:
                            await self._insert_distancia(
                                bloque, 'SITIO_SUR', int(row['Sitio 1 - Sur']), 'bloque', 'sitio'
                            )
                            await self._insert_distancia(
                                'SITIO_SUR', bloque, int(row['Sitio 1 - Sur']), 'sitio', 'bloque'
                            )
                            distancias_cargadas += 2
                        
                        # Sitio Norte
                        if 'Sitio 2 - Norte' in row and pd.notna(row['Sitio 2 - Norte']) and row['Sitio 2 - Norte'] > 0:
                            await self._insert_distancia(
                                bloque, 'SITIO_NORTE', int(row['Sitio 2 - Norte']), 'bloque', 'sitio'
                            )
                            await self._insert_distancia(
                                'SITIO_NORTE', bloque, int(row['Sitio 2 - Norte']), 'sitio', 'bloque'
                            )
                            distancias_cargadas += 2
                
                # 3. Cargar hoja "Distancias" si existe (formato ime_fm, ime_to)
                if 'Distancias' in xl.sheet_names:
                    df_dist = pd.read_excel(xl, 'Distancias')
                    logger.info("Cargando distancias desde hoja 'Distancias'...")
                    
                    for idx, row in df_dist.iterrows():
                        origen = str(row.get('ime_fm', '')).strip()
                        destino = str(row.get('ime_to', '')).strip()
                        distancia = row.get('Distancia[m]', 0)
                        
                        if origen and destino and pd.notna(distancia) and distancia > 0:
                            # Determinar tipos
                            tipo_origen = self._get_tipo_ubicacion(origen)
                            tipo_destino = self._get_tipo_ubicacion(destino)
                            
                            await self._insert_distancia(
                                origen, destino, int(distancia), tipo_origen, tipo_destino
                            )
                            distancias_cargadas += 1
                
                # 4. Cargar distancias de carga promedio si existe
                if 'CargaAvg' in xl.sheet_names:
                    df_carga = pd.read_excel(xl, 'CargaAvg')
                    logger.info("Cargando distancias promedio de carga...")
                    
                    for idx, row in df_carga.iterrows():
                        bloque = str(row.get('Bloque', '')).strip()
                        distancia = row.get('Distancia [m]', 0)
                        
                        if bloque and pd.notna(distancia) and distancia > 0:
                            # Para carga, asumimos que es desde bloque a sitio
                            await self._insert_distancia(
                                bloque, 'SITIO_CARGA', int(distancia), 'bloque', 'sitio'
                            )
                            distancias_cargadas += 1
            
            # Si NO es Costanera, solo cargar el resumen del modelo (no las distancias reales)
            else:
                logger.info("Archivo de modelo detectado, saltando carga de distancias reales")
            
            await self.db.flush()
            logger.info(f"✓ {distancias_cargadas} distancias cargadas/actualizadas")
            
            # Verificar qué se cargó
            result = await self.db.execute(
                select(func.count(DistanciaReal.id))
            )
            total_en_db = result.scalar()
            logger.info(f"Total de distancias en base de datos: {total_en_db}")
            
        except Exception as e:
            logger.error(f"Error cargando distancias: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise

    async def _insert_distancia(self, origen: str, destino: str, distancia: int, 
                               tipo_origen: str, tipo_destino: str):
        """Inserta o actualiza una distancia en la base de datos"""
        
        # Verificar si ya existe
        result = await self.db.execute(
            select(DistanciaReal).where(
                and_(
                    DistanciaReal.origen == origen,
                    DistanciaReal.destino == destino
                )
            )
        )
        existing = result.scalar_one_or_none()
        
        if not existing:
            dist = DistanciaReal(
                origen=origen,
                destino=destino,
                distancia_metros=distancia,
                tipo_origen=tipo_origen,
                tipo_destino=tipo_destino
            )
            self.db.add(dist)
            
            # Cachear para uso inmediato
            self._distancias_cache[f"{origen}_{destino}"] = distancia
        else:
            # Actualizar si cambió
            if existing.distancia_metros != distancia:
                existing.distancia_metros = distancia
                self._distancias_cache[f"{origen}_{destino}"] = distancia

    def _get_tipo_ubicacion(self, ubicacion: str) -> str:
        """Determina el tipo de ubicación basado en el código"""
        ubicacion = ubicacion.upper()
        
        if ubicacion.startswith('C') and len(ubicacion) == 2:
            return 'bloque'
        elif 'GATE' in ubicacion:
            return 'gate'
        elif 'SITIO' in ubicacion or 'SUR' in ubicacion or 'NORTE' in ubicacion:
            return 'sitio'
        elif 'PATIO' in ubicacion:
            return 'patio'
        else:
            return 'otro'
    
    async def _calculate_temporal_metrics(self, instancia_id: UUID):
        """Calcula métricas temporales agregadas usando el Escenario"""
        
        logger.info("Calculando métricas temporales...")
        
        try:
            instancia = await self.db.get(Instancia, instancia_id)
            escenario_id = instancia.escenario_id

            # 1. Obtener movimientos modelo por periodo
            modelo_result = await self.db.execute(
                select(
                    MovimientoModelo.periodo,
                    func.sum(MovimientoModelo.recepcion + MovimientoModelo.carga + 
                             MovimientoModelo.descarga + MovimientoModelo.entrega).label('total')
                ).where(MovimientoModelo.instancia_id == instancia_id)
                .group_by(MovimientoModelo.periodo)
            )
            modelo_por_periodo = {r.periodo: int(r.total or 0) for r in modelo_result.all()}
            
            # 2. Obtener movimientos reales por periodo
            real_result = await self.db.execute(
                select(
                    MovimientoReal.periodo,
                    func.count(MovimientoReal.id).label('total')
                ).where(MovimientoReal.escenario_id == escenario_id)
                .group_by(MovimientoReal.periodo)
            )
            real_por_periodo = {r.periodo: r.total for r in real_result.all()}
            
            # 3. Obtener carga de trabajo y ocupación por periodo
            carga_result = await self.db.execute(
                select(CargaTrabajo.periodo, func.sum(CargaTrabajo.carga_trabajo).label('total'))
                .where(CargaTrabajo.instancia_id == instancia_id)
                .group_by(CargaTrabajo.periodo)
            )
            carga_por_periodo = {r.periodo: int(r.total or 0) for r in carga_result.all()}
            
            ocup_result = await self.db.execute(
                select(OcupacionBloque.periodo, func.avg(OcupacionBloque.porcentaje_ocupacion).label('promedio'))
                .where(OcupacionBloque.instancia_id == instancia_id)
                .group_by(OcupacionBloque.periodo)
            )
            ocup_por_periodo = {r.periodo: float(r.promedio or 0) for r in ocup_result.all()}

            # 4. Crear métricas para los 21 periodos
            for p in range(1, 22):
                dia = ((p - 1) // 3) + 1
                turno = ((p - 1) % 3) + 1
                
                metrica = MetricaTemporal(
                    instancia_id=instancia_id,
                    periodo=p,
                    dia=dia,
                    turno=turno,
                    movimientos_real=real_por_periodo.get(p, 0),
                    movimientos_modelo=modelo_por_periodo.get(p, 0),
                    carga_trabajo=carga_por_periodo.get(p, 0),
                    ocupacion_promedio=ocup_por_periodo.get(p, 0)
                )
                self.db.add(metrica)

            await self.db.flush()
            logger.info("✓ Métricas temporales calculadas")

        except Exception as e:
            logger.error(f"Error calculando métricas temporales: {e}")
            raise
    
    async def _get_bloques_map(self) -> Dict[str, int]:
        """Obtiene mapeo de código de bloque a ID"""
        
        result = await self.db.execute(select(Bloque))
        bloques = result.scalars().all()
        return {b.codigo: b.id for b in bloques}
    
    # Actualización necesaria en el modelo Segregacion
    async def _get_or_create_segregacion(self, codigo: str, descripcion: str = '', tipo: str = None, categoria: str = None, tamano: int = None) -> Segregacion:
        """Obtiene o crea una segregación con información completa"""
        
        result = await self.db.execute(
            select(Segregacion).where(Segregacion.codigo == codigo)
        )
        segregacion = result.scalar_one_or_none()
        
        if not segregacion:
            # Si no se proporcionan tipo/categoria/tamano, intentar parsear de la descripción
            if not tipo and descripcion:
                desc_lower = descripcion.lower()
                
                # Parsear tipo
                if 'expo' in desc_lower:
                    tipo = 'expo'
                elif 'impo' in desc_lower:
                    tipo = 'impo'
                else:
                    tipo = 'desconocido'
                
                # Parsear categoría
                if 'dry' in desc_lower:
                    categoria = 'dry'
                elif 'reefer' in desc_lower:
                    categoria = 'reefer'
                elif 'empty' in desc_lower:
                    categoria = 'empty'
                else:
                    categoria = 'desconocido'
                
                # Parsear tamaño
                if '-20-' in descripcion or '20' in descripcion:
                    tamano = 20
                elif '-40-' in descripcion or '40' in descripcion:
                    tamano = 40
            
            segregacion = Segregacion(
                codigo=codigo,
                descripcion=descripcion,
                tipo=tipo or 'desconocido',
                categoria=categoria or 'desconocido',
                tamano=tamano
            )
            self.db.add(segregacion)
            await self.db.flush()
        else:
            # Actualizar si hay nueva información
            if descripcion and not segregacion.descripcion:
                segregacion.descripcion = descripcion
            if tipo and segregacion.tipo == 'desconocido':
                segregacion.tipo = tipo
            if categoria and segregacion.categoria == 'desconocido':
                segregacion.categoria = categoria
            if tamano and not segregacion.tamano:
                segregacion.tamano = tamano
        
        return segregacion
    
    async def _log_procesamiento(
        self, instancia_id: UUID, archivo: str, tipo: str,
        registros: int, estado: str, error: str = None
    ):
        """Registra log de procesamiento"""
        
        log = LogProcesamiento(
            instancia_id=instancia_id,
            archivo_nombre=Path(archivo).name,
            archivo_tipo=tipo,
            registros_procesados=registros,
            estado=estado,
            mensaje_error=error
        )
        self.db.add(log)
        await self.db.flush()

    def _log_summary(self, instancia_id: UUID, stats_resultado: Dict,
                    stats_flujos: Dict, kpis: Dict):
        """Log resumen de la carga incluyendo distancias - VERSIÓN MEJORADA"""
        
        logger.info("="*80)
        logger.info("📊 RESUMEN DE CARGA DE OPTIMIZACIÓN")
        logger.info("="*80)
        logger.info(f"Instancia ID: {instancia_id}")
        
        logger.info("\n📋 Datos cargados:")
        logger.info(f"  - Movimientos reales totales: {stats_flujos.get('total_movimientos', 0):,}")
        logger.info(f"  - Movimientos YARD: {kpis.get('movimientos_yard', 0):,}")
        logger.info(f"  - Movimientos operativos (YARD+DLVR+LOAD): {kpis.get('movimientos_operativos_real', 0):,}")
        logger.info(f"  - Movimientos optimizados (DLVR+LOAD): {kpis.get('movimientos_dlvr_modelo', 0) + kpis.get('movimientos_load_modelo', 0):,}")
        logger.info(f"  - Bloques activos: {len(stats_resultado.get('bloques_activos', set()))}")
        logger.info(f"  - Segregaciones: {kpis.get('total_segregaciones_sistema', 0)}")
        logger.info(f"  - Capacidades actualizadas: {'Sí' if stats_resultado.get('capacidades_actualizadas', False) else 'No'}")
        
        logger.info("\n📏 DISTANCIAS:")
        logger.info(f"  - Distancia total real: {kpis.get('distancia_total_real', 0):,} metros")
        logger.info(f"  - Distancia total modelo: {kpis.get('distancia_total_modelo', 0):,} metros")
        logger.info(f"  - DISTANCIA AHORRADA: {kpis.get('distancia_ahorrada', 0):,} metros")
        
        logger.info("\n  Desglose por tipo:")
        logger.info(f"  - YARD real: {kpis.get('distancia_yard', 0):,} m → modelo: 0 m (100% reducción)")
        logger.info(f"  - LOAD real: {kpis.get('distancia_load_real', 0):,} m → modelo: {kpis.get('distancia_load_modelo', 0):,} m")
        logger.info(f"  - DLVR real: {kpis.get('distancia_dlvr_real', 0):,} m → modelo: {kpis.get('distancia_dlvr_modelo', 0):,} m")
        
        logger.info("\n🎯 EFICIENCIA:")
        logger.info(f"  - Reducción de movimientos operativos: {kpis.get('porcentaje_reduccion_movimientos', 0):.1f}%")
        logger.info(f"  - Eficiencia en distancia: {kpis.get('eficiencia_ganada', 0):.2f}%")
        logger.info(f"  - Eficiencia operacional: {100 - (kpis.get('movimientos_yard', 0) / stats_flujos.get('total_movimientos', 1) * 100):.1f}% → 100%")
        
        logger.info("\n📊 OTROS KPIs:")
        logger.info(f"  - Ocupación promedio: {kpis.get('ocupacion_promedio', 0):.1f}%")
        logger.info(f"  - Ocupación máxima: {kpis.get('ocupacion_maxima', 0):.1f}%")
        logger.info(f"  - Variación de carga: {stats_resultado.get('variacion_carga', 0)}")
        logger.info(f"  - Balance de carga: {stats_resultado.get('balance_carga', 0)}")
        
        logger.info("="*80)