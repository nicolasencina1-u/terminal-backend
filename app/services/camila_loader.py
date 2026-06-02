# app/services/camila_loader.py

import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, and_, func
import logging
from uuid import UUID
import re
import json
from pathlib import Path

from app.models.optimization import Escenario, MovimientoReal, TipoMovimiento
from app.models.camila import (
    ResultadoCamila, ResumenRecursosBloque, AsignacionGrua,
    CuotaCamion, MetricaGrua, ComparacionDetallada, FlujoModelo, 
    ParametroCamila, DistribucionBloque, EstadoProcesamiento,
    TipoOperacion, TipoAsignacion, SegregacionMapping
)

logger = logging.getLogger(__name__)


class CamilaLoader:
    """Servicio optimizado para cargar y procesar datos del modelo Camila con análisis ejecutivo"""
    
    def __init__(self, db: AsyncSession):
        self.db = db
        self.parametros_cache = {}
        self.meta_productividad = 75  # cont/hora
        
    async def load_camila_results(
        self,
        resultado_filepath: str,
        instancia_filepath: Optional[str],
        flujos_real_filepath: Optional[str],
        fecha_inicio: datetime,
        semana: int,
        anio: int,
        turno: int,
        participacion: int,
        con_dispersion: bool = True,  # Siempre K por defecto
        variant: str = "magdalena",
        criterio: Optional[int] = None,
        granularidad: Optional[str] = None
    ) -> UUID:
        """Carga completa de resultados con cálculo de KPIs ejecutivos"""
        
        logger.info(f"{'='*80}")
        logger.info(f"🚀 Carga Ejecutiva Camila - Turno {turno}")
        logger.info(f"   Fecha: {fecha_inicio.date()}, Semana {semana}, P{participacion}")
        
        try:
            # 0. Obtener escenario común
            escenario = await self._get_or_create_escenario(anio, semana, participacion, con_dispersion)

            # 1. Crear o actualizar resultado
            resultado = await self._create_or_update_resultado(
                escenario.id, fecha_inicio, semana, anio, turno, participacion,
                resultado_filepath, instancia_filepath, flujos_real_filepath,
                con_dispersion, variant, criterio, granularidad
            )
            
            # 2. Cargar mapeo de segregaciones si existe
            segregacion_map = {}
            if instancia_filepath and Path(instancia_filepath).exists():
                segregacion_map = await self._load_segregacion_mapping(
                    instancia_filepath, resultado.id
                )
            
            # 3. Cargar parámetros del modelo
            if instancia_filepath and Path(instancia_filepath).exists():
                await self._load_parametros(instancia_filepath)
            
            # 4. Procesar resultado del modelo
            stats_modelo = await self._load_resultado_file(
                resultado_filepath, resultado.id, segregacion_map
            )
            
            # 5. Calcular KPIs principales
            await self._calculate_kpis_principales(resultado, stats_modelo['stats'])
            
            # 6. Calcular distribución por bloques
            await self._calculate_distribucion_bloques(resultado.id)
            
            # 7. Calcular métricas de grúas
            await self._calculate_metricas_gruas(resultado.id)
            
            # 8. Si hay datos reales, comparar
            await self._compare_with_reality(
                resultado.id, flujos_real_filepath, 
                fecha_inicio, turno
            )
            
            # Recalcular KPIs con comparación
            await self._update_kpis_con_comparacion(resultado.id)
            
            # 9. Finalizar
            resultado.estado = EstadoProcesamiento.COMPLETADO
            resultado.fecha_procesamiento = datetime.utcnow()
            
            await self.db.commit()
            
            # Log resumen ejecutivo
            await self._log_resumen_ejecutivo(resultado.id)
            
            return resultado.id
            
        except Exception as e:
            await self.db.rollback()
            logger.error(f"❌ Error en carga: {str(e)}")
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
            logger.info(f"✨ Escenario creado para Camila: {anio}-W{semana}-P{participacion}")
        else:
            logger.info(f"✅ Escenario existente para Camila: {anio}-W{semana}-P{participacion}")
            
        return escenario
    
    async def _create_or_update_resultado(
        self, escenario_id: UUID, fecha_inicio: datetime, semana: int, anio: int,
        turno: int, participacion: int,
        resultado_filepath: str, instancia_filepath: Optional[str],
        flujos_real_filepath: Optional[str], con_dispersion: bool,
        variant: str, criterio: int, granularidad: str
    ) -> ResultadoCamila:
        """Crea o actualiza un resultado vinculado a un escenario"""
        
        # Calcular día y horarios
        dia = ((turno - 1) // 3) + 1
        turno_del_dia = ((turno - 1) % 3) + 1
        hora_inicio = {1: 8, 2: 16, 3: 0}[turno_del_dia]
        
        fecha_turno = fecha_inicio + timedelta(days=dia-1, hours=hora_inicio)
        fecha_fin_turno = fecha_turno + timedelta(hours=8)
        
        # Código único
        fecha_str = fecha_inicio.strftime('%Y%m%d')
        dispersion_str = 'K' if con_dispersion else 'N'
        codigo = f"{fecha_str}_{participacion}_{dispersion_str}_T{turno:02d}_{variant}"
        if criterio: codigo += f"_c{criterio}"
        if granularidad: codigo += f"_{granularidad[:3]}"
        
        # Buscar existente
        query = select(ResultadoCamila).where(ResultadoCamila.codigo == codigo)
        result = await self.db.execute(query)
        resultado = result.scalar_one_or_none()
        
        if resultado:
            logger.info(f"♻️ Actualizando resultado existente: {codigo}")
            await self._delete_resultado_data(resultado.id)
            resultado.escenario_id = escenario_id
            resultado.fecha_procesamiento = datetime.utcnow()
            resultado.estado = EstadoProcesamiento.PROCESANDO
        else:
            logger.info(f"✨ Creando nuevo resultado: {codigo}")
            resultado = ResultadoCamila(
                codigo=codigo,
                escenario_id=escenario_id,
                fecha_inicio=fecha_turno,
                fecha_fin=fecha_fin_turno,
                anio=anio,
                semana=semana,
                dia=dia,
                turno=turno,
                turno_del_dia=turno_del_dia,
                hora_inicio=hora_inicio,
                participacion=participacion,
                con_dispersion=con_dispersion,
                variant=variant,
                criterio=criterio,
                granularidad=granularidad,
                estado=EstadoProcesamiento.PROCESANDO,
                archivo_resultado=Path(resultado_filepath).name if resultado_filepath else None,
                archivo_instancia=Path(instancia_filepath).name if instancia_filepath else None,
                archivo_flujos_real=Path(flujos_real_filepath).name if flujos_real_filepath else None
            )
            self.db.add(resultado)
            await self.db.flush()
        
        return resultado
    
    async def _delete_resultado_data(self, resultado_id: UUID):
        """Elimina datos anteriores de un resultado"""
        await self.db.execute(delete(AsignacionGrua).where(AsignacionGrua.resultado_id == resultado_id))
        await self.db.execute(delete(CuotaCamion).where(CuotaCamion.resultado_id == resultado_id))
        await self.db.execute(delete(MetricaGrua).where(MetricaGrua.resultado_id == resultado_id))
        await self.db.execute(delete(ComparacionDetallada).where(ComparacionDetallada.resultado_id == resultado_id))
        await self.db.execute(delete(FlujoModelo).where(FlujoModelo.resultado_id == resultado_id))
        await self.db.execute(delete(DistribucionBloque).where(DistribucionBloque.resultado_id == resultado_id))
        await self.db.flush()
    
    async def _load_parametros(self, filepath: str):
        """Carga parámetros del modelo"""
        try:
            xl = pd.ExcelFile(filepath)
            
            parametros_map = {
                'mu': ('Tiempo de servicio', 'minutos'),
                'W': ('Ventana de colisión', 'grúas'),
                'K': ('Duración mínima asignación', 'periodos'),
                'Rmax': ('Máximo grúas activas', 'grúas')
            }
            
            for param_code, (descripcion, unidad) in parametros_map.items():
                if param_code in xl.sheet_names:
                    df = pd.read_excel(xl, param_code, header=None)
                    if len(df) > 1:
                        valor = float(df.iloc[1, 0])
                        self.parametros_cache[param_code] = valor
                        
                        query = select(ParametroCamila).where(ParametroCamila.codigo == param_code)
                        result = await self.db.execute(query)
                        param = result.scalar_one_or_none()
                        
                        if not param:
                            param = ParametroCamila(
                                codigo=param_code,
                                descripcion=descripcion,
                                valor_default=valor,
                                valor_actual=valor,
                                unidad=unidad
                            )
                            self.db.add(param)
                        else:
                            param.valor_actual = valor
                            param.fecha_actualizacion = datetime.utcnow()
            
            await self.db.flush()
            logger.info(f"✅ Parámetros cargados: {list(self.parametros_cache.keys())}")
            
        except Exception as e:
            logger.warning(f"⚠️ Error cargando parámetros: {e}")



    async def _calculate_cuotas_camiones(self, resultado_id: UUID, hora_base: int):
        """Calcula cuotas de camiones por periodo y bloque"""
        
        # Obtener flujos y asignaciones
        flujos_result = await self.db.execute(
            select(FlujoModelo).where(FlujoModelo.resultado_id == resultado_id)
        )
        flujos = flujos_result.scalars().all()
        
        asig_result = await self.db.execute(
            select(AsignacionGrua).where(
                and_(
                    AsignacionGrua.resultado_id == resultado_id,
                    AsignacionGrua.asignada == True
                )
            )
        )
        asignaciones = asig_result.scalars().all()
        
        # Agrupar flujos por periodo-bloque
        cuotas_data = {}
        
        for flujo in flujos:
            key = (flujo.periodo, flujo.bloque_codigo)
            if key not in cuotas_data:
                cuotas_data[key] = {
                    'cantidad': 0,
                    'tipos': set(),
                    'segregaciones': set()
                }
            cuotas_data[key]['cantidad'] += flujo.cantidad
            cuotas_data[key]['tipos'].add(flujo.tipo_operacion)
            cuotas_data[key]['segregaciones'].add(flujo.segregacion_codigo)
        
        # Contar grúas por periodo-bloque
        gruas_por_pb = {}
        for asig in asignaciones:
            key = (asig.periodo, asig.bloque_codigo)
            if key not in gruas_por_pb:
                gruas_por_pb[key] = 0
            gruas_por_pb[key] += 1
        
        # Crear cuotas
        mu = self.parametros_cache.get('mu', 30)
        
        for (periodo, bloque), data in cuotas_data.items():
            gruas = gruas_por_pb.get((periodo, bloque), 0)
            capacidad = int((60 / mu) * gruas) if gruas > 0 else 0
            hora_real = (hora_base + periodo - 1) % 24
            
            # Determinar tipo operación
            if len(data['tipos']) == 1:
                tipo_op = list(data['tipos'])[0]
            else:
                tipo_op = TipoOperacion.MIXTO
            
            raw_pct = (data['cantidad'] / capacidad * 100) if capacidad > 0 else 0
            
            cuota = CuotaCamion(
                resultado_id=resultado_id,
                periodo=periodo,
                hora_real=hora_real,
                bloque_codigo=bloque,
                cuota_modelo=data['cantidad'],
                capacidad_maxima=capacidad,
                gruas_asignadas=gruas,
                utilizacion_modelo_pct=round(min(raw_pct, 999.99), 2),
                tipo_operacion=tipo_op,
                segregaciones_incluidas=list(data['segregaciones'])
            )
            self.db.add(cuota)
        
        await self.db.flush()
        
         
    async def _load_resultado_file(self, filepath: str, resultado_id: UUID, 
                                  segregacion_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Carga y procesa archivo de resultados con análisis ejecutivo"""
        
        logger.info("📂 Procesando archivo de resultados...")
        
        try:
            df = pd.read_excel(filepath, header=None, names=['var', 'idx', 'val'])
            
            stats = {
                'total_registros': len(df),
                'total_movimientos': 0,
                'flujos_por_tipo': {'fr': 0, 'fe': 0, 'fc': 0, 'fd': 0},
                'movimientos_por_bloque': {},
                'movimientos_por_periodo': {},
                'gruas_activas': set(),
                'bloques_visitados': set(),
                'asignaciones_grua': {},
                'min_diff_val': None
            }
            
            batch_flujos = []
            batch_asignaciones = []
            batch_resumen_recursos = []
            
            asignaciones_dict = {}
            datos_bloque_temp = {}
            
            # Obtener hora inicio del turno
            resultado = await self.db.get(ResultadoCamila, resultado_id)
            hora_base = resultado.hora_inicio
            
            # Procesar cada fila
            for idx, row in df.iterrows():
                try:
                    if pd.isna(row['var']) or pd.isna(row['val']):
                        continue
                    
                    var_name = str(row['var']).strip()
                    var_index = str(row['idx']).strip()
                    var_value = float(row['val'])
                    
                    if var_value == 0:
                        continue
                    
                    # Procesar flujos
                    if var_name in ['fr_sbt', 'fe_sbt', 'fc_sbt', 'fd_sbt']:
                        match = re.match(r"\('([^']+)',\s*'([^']+)',\s*(\d+)\)", var_index)
                        if match:
                            segregacion = match.group(1).upper()
                            bloque = match.group(2).upper().replace('B', 'C')
                            periodo = int(match.group(3))
                            cantidad = int(var_value)
                            
                            # Calcular hora real
                            hora_real = (hora_base + periodo - 1) % 24
                            
                            # Actualizar estadísticas
                            stats['bloques_visitados'].add(bloque)
                            stats['total_movimientos'] += cantidad
                            tipo_key = var_name.split('_')[0]
                            if tipo_key in stats['flujos_por_tipo']:
                                stats['flujos_por_tipo'][tipo_key] += cantidad
                            
                            # Por bloque
                            if bloque not in stats['movimientos_por_bloque']:
                                stats['movimientos_por_bloque'][bloque] = 0
                            stats['movimientos_por_bloque'][bloque] += cantidad
                            
                            # Por periodo
                            if periodo not in stats['movimientos_por_periodo']:
                                stats['movimientos_por_periodo'][periodo] = 0
                            stats['movimientos_por_periodo'][periodo] += cantidad
                            
                            # Tipo de operación
                            tipo_map = {
                                'fr': TipoOperacion.RECEPCION,
                                'fe': TipoOperacion.ENTREGA,
                                'fc': TipoOperacion.CARGA,
                                'fd': TipoOperacion.DESCARGA
                            }
                            tipo_flujo = var_name.split('_')[0]
                            
                            flujo = FlujoModelo(
                                resultado_id=resultado_id,
                                tipo_flujo=tipo_flujo,
                                segregacion_codigo=segregacion,
                                bloque_codigo=bloque,
                                periodo=periodo,
                                hora_real=hora_real,
                                cantidad=cantidad,
                                tipo_operacion=tipo_map[tipo_flujo]
                            )
                            batch_flujos.append(flujo)

                    # Procesar asignaciones de grúas
                    elif var_name == 'ygbt' and var_value == 1:
                        match = re.match(r"\('([^']+)',\s*'([^']+)',\s*(\d+)\)", var_index)
                        if match:
                            grua = match.group(1).upper()
                            grua_id, tipo_equipo = self.obtener_id_grua(grua)
                            bloque = match.group(2).upper().replace('B', 'C')
                            periodo = int(match.group(3))
                            hora_real = (hora_base + periodo - 1) % 24
                            
                            key = (grua_id, tipo_equipo, bloque, periodo)
                            if key not in asignaciones_dict:
                                asignaciones_dict[key] = {
                                    'asignada': False,
                                    'activada': False,
                                    'movimientos': 0,
                                    'hora_real': hora_real
                                }
                            asignaciones_dict[key]['asignada'] = True
                            stats['gruas_activas'].add(grua_id)
                    
                    # Procesar activaciones
                    elif var_name == 'alpha_gbt' and var_value == 1:
                        match = re.match(r"\('([^']+)',\s*'([^']+)',\s*(\d+)\)", var_index)
                        if match:
                            grua = match.group(1).upper()
                            grua_id, tipo_equipo = self.obtener_id_grua(grua)
                            bloque = match.group(2).upper().replace('B', 'C')
                            periodo = int(match.group(3))

                            key = (grua_id, tipo_equipo, bloque, periodo)
                            if key in asignaciones_dict:
                                asignaciones_dict[key]['activada'] = True
                    
                    # Procesar NUEVAS VARIABLES (nRTG, nRS, etc.) - MODELO PIPELINE
                    elif var_name in ['nRTG', 'nRS', 'aRTG', 'aRS']:
                        clean_index = var_index.replace("(", "").replace(")", "").replace("'", "")
                        parts = [p.strip() for p in clean_index.split(",")]
                        
                        if len(parts) >= 2:
                            bloque_raw = parts[0]
                            bloque = bloque_raw.replace('B', 'C') # Estandarizar nombre bloque
                            periodo = int(parts[1])
                            
                            key_resumen = (bloque, periodo)
                            
                            if key_resumen not in datos_bloque_temp:
                                datos_bloque_temp[key_resumen] = {}
                            
                            mapa_cols = {
                                'nRTG': 'n_rtg', 'nRS': 'n_rs',
                                'aRTG': 'a_rtg', 'aRS': 'a_rs'
                            }
                            col_db = mapa_cols.get(var_name)
                            if col_db:
                                datos_bloque_temp[key_resumen][col_db] = int(var_value)

                    # Nuevo KPI Pipeline
                    elif var_name == 'min_diff_val':
                        stats['min_diff_val'] = var_value
                        
                except Exception as e:
                    logger.warning(f"Error en fila {idx}: {str(e)}")
            
            # Guardar flujos
            if batch_flujos:
                self.db.add_all(batch_flujos)
                await self.db.flush()
            
            # Calcular movimientos por asignación
            for flujo in batch_flujos:
                for (grua_id, tipo, bloque, periodo), asig_data in asignaciones_dict.items():
                    if bloque == flujo.bloque_codigo and periodo == flujo.periodo and asig_data['asignada']:
                        gruas_en_bloque_periodo = sum(
                            1 for (g, t, b, p), data in asignaciones_dict.items() 
                            if b == bloque and p == periodo and data['asignada']
                        )
                        if gruas_en_bloque_periodo > 0:
                            asig_data['movimientos'] += flujo.cantidad // gruas_en_bloque_periodo
            
            # Crear asignaciones
            for (grua_id, tipo, bloque, periodo), asig_data in asignaciones_dict.items():
                asignacion = AsignacionGrua(
                    resultado_id=resultado_id,
                    grua_id=grua_id,
                    tipo_equipo=tipo,
                    bloque_codigo=bloque,
                    periodo=periodo,
                    hora_real=asig_data['hora_real'],
                    asignada=asig_data['asignada'],
                    activada=asig_data['activada'],
                    movimientos_asignados=asig_data['movimientos'],
                    tipo_asignacion=TipoAsignacion.REGULAR
                )
                batch_asignaciones.append(asignacion)
            
            if batch_asignaciones:
                self.db.add_all(batch_asignaciones)
                await self.db.flush()
                
            for (bloque, periodo), data in datos_bloque_temp.items():
                nuevo_resumen = ResumenRecursosBloque(
                    resultado_id=resultado_id,
                    bloque_codigo=bloque,
                    periodo=periodo,
                    n_rtg=data.get('n_rtg', 0),
                    n_rs=data.get('n_rs', 0),
                    a_rtg=data.get('a_rtg', 0),
                    a_rs=data.get('a_rs', 0)
                )
                batch_resumen_recursos.append(nuevo_resumen)

            if batch_resumen_recursos:
                self.db.add_all(batch_resumen_recursos)
                await self.db.flush()

            # Guardar KPI
            if stats['min_diff_val'] is not None:
                resultado.min_diff_val = stats['min_diff_val']
                self.db.add(resultado) # Asegurar que se actualice
            
            # Calcular cuotas
            await self._calculate_cuotas_camiones(resultado_id, hora_base)
            
            # Convertir sets a listas
            stats['gruas_activas'] = list(stats['gruas_activas'])
            stats['bloques_visitados'] = list(stats['bloques_visitados'])
            
            logger.info(f"✅ Procesados {stats['total_movimientos']} movimientos")
            logger.info(f"   Bloques activos: {len(stats['bloques_visitados'])}")
            logger.info(f"   Grúas utilizadas: {len(stats['gruas_activas'])}")
            
            return {
                "stats": stats,
                "batch_flujos": batch_flujos,
                "asignaciones_dict": asignaciones_dict,
                "datos_resumen_bloque": datos_bloque_temp
            }
            
        except Exception as e:
            logger.error(f"❌ Error cargando resultados: {e}")
            raise
    
    async def _calculate_kpis_principales(self, resultado: ResultadoCamila, stats: Dict):
        """Calcula KPIs principales del dashboard ejecutivo"""
        
        # PRODUCTIVIDAD
        resultado.total_movimientos_modelo = stats['total_movimientos']
        resultado.productividad_hora = round(stats['total_movimientos'] / 8, 2)
        resultado.productividad_meta_pct = round(
            (resultado.productividad_hora / self.meta_productividad) * 100, 2
        )
        
        # BALANCE DE FLUJOS
        resultado.movimientos_carga = stats['flujos_por_tipo']['fc']
        resultado.movimientos_descarga = stats['flujos_por_tipo']['fd']
        resultado.movimientos_entrega = stats['flujos_por_tipo']['fe']
        resultado.movimientos_recepcion = stats['flujos_por_tipo']['fr']
        
        resultado.total_entradas = resultado.movimientos_recepcion + resultado.movimientos_descarga
        resultado.total_salidas = resultado.movimientos_carga + resultado.movimientos_entrega
        resultado.balance_flujos = resultado.total_entradas - resultado.total_salidas
        
        # Categorizar balance
        if abs(resultado.balance_flujos) <= 50:
            resultado.balance_categoria = 'BALANCEADO'
        elif resultado.balance_flujos < -200:
            resultado.balance_categoria = 'DESBALANCEADO_SALIDAS'
        elif resultado.balance_flujos > 200:
            resultado.balance_categoria = 'DESBALANCEADO_ENTRADAS'
        else:
            resultado.balance_categoria = 'MODERADO'
        
        # UTILIZACIÓN
        resultado.total_gruas_utilizadas = len(stats['gruas_activas'])
        resultado.utilizacion_gruas_pct = round((resultado.total_gruas_utilizadas / 12) * 100, 2)
        
        # Bloque más utilizado
        if stats['movimientos_por_bloque']:
            bloque_max = max(stats['movimientos_por_bloque'].items(), key=lambda x: x[1])
            resultado.bloque_mas_utilizado = bloque_max[0]
            resultado.concentracion_maxima_pct = round(
                (bloque_max[1] / stats['total_movimientos']) * 100, 2
            )
        
        await self.db.flush()
    
    async def _calculate_distribucion_bloques(self, resultado_id: UUID):
        """Calcula distribución de actividad por bloque"""
        
        # Obtener flujos agrupados por bloque
        query = select(
            FlujoModelo.bloque_codigo,
            FlujoModelo.tipo_flujo,
            func.sum(FlujoModelo.cantidad).label('total')
        ).where(
            FlujoModelo.resultado_id == resultado_id
        ).group_by(FlujoModelo.bloque_codigo, FlujoModelo.tipo_flujo)
        
        result = await self.db.execute(query)
        flujos_por_bloque = result.all()
        
        # Organizar por bloque
        bloques_data = {}
        total_movimientos = 0
        
        for row in flujos_por_bloque:
            bloque = row.bloque_codigo
            tipo = row.tipo_flujo
            cantidad = row.total
            
            if bloque not in bloques_data:
                bloques_data[bloque] = {
                    'total': 0,
                    'fc': 0, 'fd': 0, 'fe': 0, 'fr': 0
                }
            
            bloques_data[bloque][tipo] = cantidad
            bloques_data[bloque]['total'] += cantidad
            total_movimientos += cantidad
        
        # Crear registros de distribución
        for bloque, data in bloques_data.items():
            dist = DistribucionBloque(
                resultado_id=resultado_id,
                bloque_codigo=bloque,
                movimientos_total=data['total'],
                porcentaje_actividad=round((data['total'] / total_movimientos * 100), 2) if total_movimientos > 0 else 0,
                movimientos_carga=data['fc'],
                movimientos_descarga=data['fd'],
                movimientos_entrega=data['fe'],
                movimientos_recepcion=data['fr']
            )
            self.db.add(dist)
        
        await self.db.flush()
    def _son_bloques_adyacentes(self, bloque1: str, bloque2: str) -> bool:
        """Determina si dos bloques son adyacentes"""
        # Extraer números de bloque (C1 -> 1)
        try:
            num1 = int(bloque1[1:])
            num2 = int(bloque2[1:])
            return abs(num1 - num2) == 1
        except:
            return False
        
        
    async def _calculate_metricas_gruas(self, resultado_id: UUID):
        """Calcula métricas de productividad por grúa con KPIs adicionales"""
        
        # Obtener asignaciones
        asig_result = await self.db.execute(
            select(AsignacionGrua).where(
                AsignacionGrua.resultado_id == resultado_id
            )
        )
        asignaciones = asig_result.scalars().all()
        
        # Obtener resultado para actualizar
        resultado = await self.db.get(ResultadoCamila, resultado_id)
        
        # Agrupar por grúa
        mu = self.parametros_cache.get('mu', 30)
        movimientos_por_grua = []
        
        for grua_id in range(1, 13):  # 12 grúas
            asig_grua = [a for a in asignaciones if a.grua_id == grua_id]
            
            movimientos = sum(a.movimientos_asignados for a in asig_grua)
            bloques_visitados = len(set(a.bloque_codigo for a in asig_grua))
            periodos_activa = len(set(a.periodo for a in asig_grua if a.asignada))
            
            # Calcular tiempos
            tiempo_productivo = (movimientos * mu) / 60  # horas
            tiempo_total = 8  # turno de 8 horas
            tiempo_improductivo = max(0, tiempo_total - tiempo_productivo)
            utilizacion = (tiempo_productivo / tiempo_total * 100) if tiempo_total > 0 else 0
            
            movimientos_por_grua.append(movimientos)
            
            metrica = MetricaGrua(
                resultado_id=resultado_id,
                grua_id=grua_id,
                movimientos_modelo=movimientos,
                movimientos_por_hora=round(movimientos / 8, 2),
                bloques_visitados=bloques_visitados,
                periodos_activa=periodos_activa,
                tiempo_productivo_hrs=round(tiempo_productivo, 2),
                tiempo_improductivo_hrs=round(tiempo_improductivo, 2),
                utilizacion_pct=round(utilizacion, 2),
                cumple_meta=(movimientos >= 50)  # Meta: 50 mov/grúa
            )
            self.db.add(metrica)
        
        # MÉTRICAS AGREGADAS A NIVEL DE RESULTADO
        
        # 2. Productividad promedio por grúa activa
        gruas_activas = [m for m in movimientos_por_grua if m > 0]
        if gruas_activas:
            productividad_promedio_grua = sum(gruas_activas) / len(gruas_activas) / 8
            resultado.productividad_promedio_grua = round(productividad_promedio_grua, 2)
        else:
            resultado.productividad_promedio_grua = 0
        
        # 3. Distribución de carga (coeficiente de variación)
        if len(gruas_activas) > 1:
            media = np.mean(gruas_activas)
            desviacion = np.std(gruas_activas)
            resultado.distribucion_carga_cv = round((desviacion / media * 100), 2) if media > 0 else 0
        else:
            resultado.distribucion_carga_cv = 0
        
        # 4. Factor de interferencia (grúas en bloques adyacentes)
        interferencias = 0
        asignaciones_por_periodo = {}
        
        for asig in asignaciones:
            if asig.asignada:
                key = asig.periodo
                if key not in asignaciones_por_periodo:
                    asignaciones_por_periodo[key] = []
                asignaciones_por_periodo[key].append((asig.grua_id, asig.bloque_codigo))
        
        for periodo, asigs in asignaciones_por_periodo.items():
            bloques_periodo = {}
            for grua_id, bloque in asigs:
                if bloque not in bloques_periodo:
                    bloques_periodo[bloque] = []
                bloques_periodo[bloque].append(grua_id)
            
            # Verificar bloques adyacentes
            bloques = list(bloques_periodo.keys())
            for i in range(len(bloques)):
                for j in range(i+1, len(bloques)):
                    if self._son_bloques_adyacentes(bloques[i], bloques[j]):
                        interferencias += len(bloques_periodo[bloques[i]]) * len(bloques_periodo[bloques[j]])
        
        total_asignaciones_activas = len([a for a in asignaciones if a.asignada])
        resultado.factor_interferencia_pct = round(
            (interferencias / total_asignaciones_activas * 100), 2
        ) if total_asignaciones_activas > 0 else 0
        
        # 5. Índice de reasignaciones
        cambios_bloque = 0
        for grua_id in range(1, 13):
            asig_grua = sorted(
                [a for a in asignaciones if a.grua_id == grua_id and a.asignada],
                key=lambda x: x.periodo
            )
            
            for i in range(1, len(asig_grua)):
                if asig_grua[i].bloque_codigo != asig_grua[i-1].bloque_codigo:
                    cambios_bloque += 1
        
        resultado.indice_reasignaciones_pct = round(
            (cambios_bloque / resultado.total_movimientos_modelo * 100), 2
        ) if resultado.total_movimientos_modelo > 0 else 0
        
        # 6. Tiempo de respuesta (bloques sin grúa cuando hay demanda)
        flujos_result = await self.db.execute(
            select(FlujoModelo).where(FlujoModelo.resultado_id == resultado_id)
        )
        flujos = flujos_result.scalars().all()
        
        bloques_sin_grua = 0
        bloques_con_demanda = set()  # Usar set para evitar duplicados
        
        for flujo in flujos:
            if flujo.cantidad > 0:
                key = (flujo.bloque_codigo, flujo.periodo)
                bloques_con_demanda.add(key)
        
        # Verificar cuáles no tienen grúa
        for bloque, periodo in bloques_con_demanda:
            tiene_grua = any(
                a for a in asignaciones 
                if a.bloque_codigo == bloque 
                and a.periodo == periodo 
                and a.asignada
            )
            if not tiene_grua:
                bloques_sin_grua += 1
        
        resultado.tiempo_respuesta_pct = round(
            (bloques_sin_grua / len(bloques_con_demanda) * 100), 2
        ) if len(bloques_con_demanda) > 0 else 0
        
        await self.db.flush()
    

    async def _compare_with_reality(self, resultado_id: UUID, flujos_filepath: Optional[str], 
                                fecha_inicio: datetime, turno: int):
        """Compara resultados del modelo con datos reales (priorizando DB)"""
        
        logger.info("🔍 Comparación con datos reales...")
        
        try:
            resultado = await self.db.get(ResultadoCamila, resultado_id)
            escenario_id = resultado.escenario_id
            
            movs_db_query = await self.db.execute(
                select(
                    MovimientoReal.tipo_movimiento,
                    func.count(MovimientoReal.id).label('cantidad')
                ).where(
                    and_(
                        MovimientoReal.escenario_id == escenario_id,
                        MovimientoReal.periodo == turno
                    )
                ).group_by(MovimientoReal.tipo_movimiento)
            )
            
            movs_db = {row.tipo_movimiento.value: row.cantidad for row in movs_db_query}
            
            if sum(movs_db.values()) > 0:
                logger.info("✅ Usando datos reales desde la Base de Datos")
                totales_reales = {
                    'fc': movs_db.get('LOAD', 0),
                    'fe': movs_db.get('DLVR', 0),
                    'fr': movs_db.get('RECV', 0),
                    'fd': movs_db.get('DSCH', 0)
                }
            elif flujos_filepath and Path(flujos_filepath).exists():
                logger.info("⚠️ No hay datos en DB, cargando desde Excel de Optimización...")
                mag_path = Path(flujos_filepath)
                
                # Cargar hojas de Optimización
                flujos_reales = {
                    'fc': pd.read_excel(mag_path, sheet_name="Cargar"),
                    'fe': pd.read_excel(mag_path, sheet_name="Entregar"),
                    'fr': pd.read_excel(mag_path, sheet_name="Recibir"),
                    'fd': pd.read_excel(mag_path, sheet_name="Descargar")
                }
                
                totales_reales = {}
                for tipo, df in flujos_reales.items():
                    df_turno = df[df['Periodo'] == turno] if 'Periodo' in df.columns else df
                    col_valor = next((col for col in ['Cargar', 'Entregar', 'Recibir', 'Descargar'] if col in df_turno.columns), None)
                    totales_reales[tipo] = int(df_turno[col_valor].sum()) if col_valor else 0
            else:
                logger.warning("❌ No se encontraron datos reales ni en DB ni en archivo")
                return

            # Obtener totales del modelo
            flujos_mod_query = await self.db.execute(
                select(FlujoModelo.tipo_flujo, func.sum(FlujoModelo.cantidad).label('total'))
                .where(FlujoModelo.resultado_id == resultado_id)
                .group_by(FlujoModelo.tipo_flujo)
            )
            totales_modelo = {row.tipo_flujo: row.total for row in flujos_mod_query}
            
            # Actualizar resultado principal
            resultado.movimientos_carga = totales_reales.get('fc', 0)
            resultado.movimientos_descarga = totales_reales.get('fd', 0)
            resultado.movimientos_entrega = totales_reales.get('fe', 0)
            resultado.movimientos_recepcion = totales_reales.get('fr', 0)
            resultado.total_movimientos_real = sum(totales_reales.values())
            
            # Crear comparaciones detalladas
            for tipo in ['fc', 'fd', 'fe', 'fr']:
                val_mod = totales_modelo.get(tipo, 0)
                val_real = totales_reales.get(tipo, 0)
                
                comparacion = ComparacionDetallada(
                    resultado_id=resultado_id,
                    tipo_comparacion='FLUJO',
                    subtipo=tipo.upper(),
                    valor_modelo=val_mod,
                    valor_real=val_real,
                    match_exacto=(val_mod == val_real),
                    diferencia_absoluta=val_real - val_mod,
                    precision_pct=min(val_mod, val_real) / max(val_mod, val_real) * 100 if max(val_mod, val_real) > 0 else 100,
                    categoria_diferencia=self._categorizar_diferencia(val_mod, val_real)
                )
                self.db.add(comparacion)
            
            await self.db.flush()
            
        except Exception as e:
            logger.error(f"❌ Error en comparación: {e}")
            raise


    async def _process_comparaciones_por_bloque(self, resultado_id: UUID, flujos_reales: Dict, turno: int):
        """Procesa comparaciones detalladas por bloque usando datos de Optimización"""
        
        # Obtener distribuciones del modelo
        dist_modelo = await self.db.execute(
            select(DistribucionBloque).where(
                DistribucionBloque.resultado_id == resultado_id
            )
        )
        distribuciones = {d.bloque_codigo: d for d in dist_modelo.scalars().all()}
        
        # Procesar cada tipo de flujo
        for tipo, df_real in flujos_reales.items():
            # Filtrar por turno
            df_turno = df_real[df_real['Periodo'] == turno] if 'Periodo' in df_real.columns else df_real
            
            if 'Bloque' not in df_turno.columns:
                continue
            
            # Agrupar por bloque
            col_valor = None
            for col in ['Cargar', 'Entregar', 'Recibir', 'Descargar']:
                if col in df_turno.columns:
                    col_valor = col
                    break
            
            if not col_valor:
                continue
            
            real_por_bloque = df_turno.groupby('Bloque')[col_valor].sum()
            
            # Actualizar distribuciones
            for bloque, valor_real in real_por_bloque.items():
                if bloque in distribuciones:
                    dist = distribuciones[bloque]
                    
                    # Actualizar valores reales por tipo
                    if tipo == 'fc':
                        dist.movimientos_carga = int(valor_real)
                    elif tipo == 'fd':
                        dist.movimientos_descarga = int(valor_real)
                    elif tipo == 'fe':
                        dist.movimientos_entrega = int(valor_real)
                    elif tipo == 'fr':
                        dist.movimientos_recepcion = int(valor_real)
                    
                    # Recalcular total real
                    dist.movimientos_real = (
                        (dist.movimientos_carga or 0) + 
                        (dist.movimientos_descarga or 0) + 
                        (dist.movimientos_entrega or 0) + 
                        (dist.movimientos_recepcion or 0)
                    )
                    
                    dist.diferencia_absoluta = dist.movimientos_real - dist.movimientos_total
                    dist.diferencia_porcentual = round(
                        (dist.diferencia_absoluta / dist.movimientos_total * 100), 2
                    ) if dist.movimientos_total > 0 else 0
                    dist.match_perfecto = (dist.movimientos_real == dist.movimientos_total)
        
        await self.db.flush()



    
    async def _process_comparaciones(self, resultado_id: UUID, df_real: pd.DataFrame):
        """Procesa comparaciones detalladas modelo vs real"""
        
        # Mapear tipos de movimiento
        tipo_map = {
            'RECV': 'fr',  # recepción
            'DLVR': 'fe',  # entrega
            'LOAD': 'fc',  # carga
            'DSCH': 'fd'   # descarga
        }
        
        # COMPARACIÓN POR TIPO DE FLUJO
        flujos_modelo = await self.db.execute(
            select(
                FlujoModelo.tipo_flujo,
                func.sum(FlujoModelo.cantidad).label('total')
            ).where(
                FlujoModelo.resultado_id == resultado_id
            ).group_by(FlujoModelo.tipo_flujo)
        )
        
        for row in flujos_modelo:
            tipo = row.tipo_flujo
            valor_modelo = row.total
            
            # Contar en datos reales
            tipos_reales = [k for k, v in tipo_map.items() if v == tipo]
            valor_real = len(df_real[df_real['ime_move_kind'].isin(tipos_reales)])
            
            comparacion = ComparacionDetallada(
                resultado_id=resultado_id,
                tipo_comparacion='FLUJO',
                subtipo=tipo.upper(),
                valor_modelo=valor_modelo,
                valor_real=valor_real,
                match_exacto=(valor_modelo == valor_real),
                diferencia_absoluta=valor_real - valor_modelo,
                diferencia_porcentual=((valor_real - valor_modelo) / valor_modelo * 100) if valor_modelo > 0 else 0,
                precision_pct=min(valor_modelo, valor_real) / max(valor_modelo, valor_real) * 100 if max(valor_modelo, valor_real) > 0 else 0,
                categoria_diferencia=self._categorizar_diferencia(valor_modelo, valor_real),
                requiere_accion=abs(valor_real - valor_modelo) > valor_modelo * 0.2
            )
            self.db.add(comparacion)
        
        # COMPARACIÓN POR BLOQUE
        # Mapear bloques en datos reales
        def mapear_bloque(row):
            bloques_costanera = ['C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9']
            if row['ime_fm'] in bloques_costanera:
                return row['ime_fm']
            elif row['ime_to'] in bloques_costanera:
                return row['ime_to']
            else:
                return None
        
        df_real['bloque'] = df_real.apply(mapear_bloque, axis=1)
        df_real_costanera = df_real[df_real['bloque'].notna()]
        
        # Obtener distribuciones del modelo
        dist_modelo = await self.db.execute(
            select(DistribucionBloque).where(
                DistribucionBloque.resultado_id == resultado_id
            )
        )
        
        for dist in dist_modelo.scalars().all():
            valor_modelo = dist.movimientos_total
            valor_real = len(df_real_costanera[df_real_costanera['bloque'] == dist.bloque_codigo])
            
            comparacion = ComparacionDetallada(
                resultado_id=resultado_id,
                tipo_comparacion='BLOQUE',
                subtipo=dist.bloque_codigo,
                valor_modelo=valor_modelo,
                valor_real=valor_real,
                match_exacto=(valor_modelo == valor_real),
                diferencia_absoluta=valor_real - valor_modelo,
                diferencia_porcentual=((valor_real - valor_modelo) / valor_modelo * 100) if valor_modelo > 0 else 0,
                precision_pct=min(valor_modelo, valor_real) / max(valor_modelo, valor_real) * 100 if max(valor_modelo, valor_real) > 0 else 0,
                categoria_diferencia=self._categorizar_diferencia(valor_modelo, valor_real),
                requiere_accion=abs(valor_real - valor_modelo) > valor_modelo * 0.2
            )
            self.db.add(comparacion)
        
        await self.db.flush()
    
    async def _update_cuotas_reales(self, resultado_id: UUID, df_real: pd.DataFrame):
        """Actualiza cuotas con movimientos reales"""
        
        # Mapear bloques
        def mapear_bloque(row):
            bloques_costanera = ['C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9']
            if row['ime_fm'] in bloques_costanera:
                return row['ime_fm']
            elif row['ime_to'] in bloques_costanera:
                return row['ime_to']
            else:
                return None
        
        df_real['bloque'] = df_real.apply(mapear_bloque, axis=1)
        
        # Obtener cuotas
        cuotas_result = await self.db.execute(
            select(CuotaCamion).where(CuotaCamion.resultado_id == resultado_id)
        )
        cuotas = cuotas_result.scalars().all()
        
        for cuota in cuotas:
            # Contar movimientos reales
            movimientos_reales = len(
                df_real[
                    (df_real['periodo'] == cuota.periodo) &
                    (df_real['bloque'] == cuota.bloque_codigo)
                ]
            )
            
            cuota.movimientos_reales = movimientos_reales
            cuota.utilizacion_real_pct = round(
                (movimientos_reales / cuota.capacidad_maxima * 100), 2
            ) if cuota.capacidad_maxima > 0 else 0
            
            cuota.brecha_absoluta = movimientos_reales - cuota.cuota_modelo
            cuota.cumplimiento_pct = round(
                (movimientos_reales / cuota.cuota_modelo * 100), 2
            ) if cuota.cuota_modelo > 0 else 0
        
        await self.db.flush()
    
    async def _update_distribuciones_reales(self, resultado_id: UUID, df_real: pd.DataFrame):
        """Actualiza distribuciones de bloques con datos reales"""
        
        # Mapear bloques
        def mapear_bloque(row):
            bloques_costanera = ['C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9']
            if row['ime_fm'] in bloques_costanera:
                return row['ime_fm']
            elif row['ime_to'] in bloques_costanera:
                return row['ime_to']
            else:
                return None
        
        df_real['bloque'] = df_real.apply(mapear_bloque, axis=1)
        
        # Obtener distribuciones
        dist_result = await self.db.execute(
            select(DistribucionBloque).where(
                DistribucionBloque.resultado_id == resultado_id
            )
        )
        distribuciones = dist_result.scalars().all()
        
        for dist in distribuciones:
            movimientos_real = len(df_real[df_real['bloque'] == dist.bloque_codigo])
            
            dist.movimientos_real = movimientos_real
            dist.diferencia_absoluta = movimientos_real - dist.movimientos_total
            dist.diferencia_porcentual = round(
                ((movimientos_real - dist.movimientos_total) / dist.movimientos_total * 100), 2
            ) if dist.movimientos_total > 0 else 0
            dist.match_perfecto = (movimientos_real == dist.movimientos_total)
        
        await self.db.flush()
    
    async def _update_kpis_con_comparacion(self, resultado_id: UUID):
        """Actualiza KPIs principales con datos de comparación"""
        
        resultado = await self.db.get(ResultadoCamila, resultado_id)
        
        # Obtener totales reales
        comp_result = await self.db.execute(
            select(ComparacionDetallada).where(
                and_(
                    ComparacionDetallada.resultado_id == resultado_id,
                    ComparacionDetallada.tipo_comparacion == 'FLUJO'
                )
            )
        )
        comparaciones = comp_result.scalars().all()
        
        total_real = sum(c.valor_real for c in comparaciones)
        total_modelo = sum(c.valor_modelo for c in comparaciones)
        
        resultado.total_movimientos_real = total_real
        
        # Calcular precisión global
        if total_modelo > 0 and total_real > 0:
            resultado.precision_global = round(
                min(total_modelo, total_real) / max(total_modelo, total_real) * 100, 2
            )
        
        # Calcular score de coincidencia
        total_comparaciones = await self.db.execute(
            select(func.count(ComparacionDetallada.id)).where(
                ComparacionDetallada.resultado_id == resultado_id
            )
        )
        total_comp = total_comparaciones.scalar()
        
        matches = await self.db.execute(
            select(func.count(ComparacionDetallada.id)).where(
                and_(
                    ComparacionDetallada.resultado_id == resultado_id,
                    ComparacionDetallada.match_exacto == True
                )
            )
        )
        total_matches = matches.scalar()
        
        resultado.score_coincidencia = round((total_matches / total_comp * 100), 2) if total_comp > 0 else 0
        
        # Calcular ahorro (simulado para ejemplo)
        # En un caso real, esto vendría de un análisis de distancias
        if resultado.precision_global and resultado.precision_global > 90:
            resultado.ahorro_distancia_m = 300
            resultado.ahorro_tiempo_min = 10
            resultado.ahorro_porcentual = 1.7
        elif resultado.precision_global and resultado.precision_global > 80:
            resultado.ahorro_distancia_m = 200
            resultado.ahorro_tiempo_min = 7
            resultado.ahorro_porcentual = 1.2
        else:
            resultado.ahorro_distancia_m = 100
            resultado.ahorro_tiempo_min = 3
            resultado.ahorro_porcentual = 0.6
        
        await self.db.flush()
    
    def _categorizar_diferencia(self, valor_modelo: int, valor_real: int) -> str:
        """Categoriza la diferencia entre modelo y realidad"""
        if valor_modelo == 0 and valor_real == 0:
            return 'PERFECTA'
        elif valor_modelo == valor_real:
            return 'PERFECTA'
        elif valor_modelo == 0 or valor_real == 0:
            return 'CRITICA'
        
        diferencia_pct = abs((valor_real - valor_modelo) / valor_modelo * 100)
        
        if diferencia_pct < 5:
            return 'MENOR'
        elif diferencia_pct < 20:
            return 'MODERADA'
        else:
            return 'CRITICA'
    
    async def _load_segregacion_mapping(self, instancia_filepath: str, resultado_id: UUID) -> Dict[str, str]:
        """Carga el mapeo de segregaciones desde la instancia"""
        
        try:
            xl = pd.ExcelFile(instancia_filepath)
            segregacion_map = {}
            
            if 'S' in xl.sheet_names:
                df_s = pd.read_excel(xl, 'S')
                
                # Limpiar registros anteriores
                await self.db.execute(
                    delete(SegregacionMapping).where(
                        SegregacionMapping.resultado_id == resultado_id
                    )
                )
                
                batch_mappings = []
                
                for _, row in df_s.iterrows():
                    if pd.notna(row.get('S')) and pd.notna(row.get('Segregacion')):
                        codigo = str(row['S']).strip().upper()
                        nombre = str(row['Segregacion']).strip()
                        
                        tipo = 'EXPORT' if 'expo' in nombre.lower() else 'IMPORT'
                        
                        size = None
                        if '-20-' in nombre:
                            size = 20
                        elif '-40-' in nombre:
                            size = 40
                        
                        mapping = SegregacionMapping(
                            resultado_id=resultado_id,
                            codigo=codigo,
                            nombre=nombre,
                            tipo=tipo,
                            size=size
                        )
                        batch_mappings.append(mapping)
                        segregacion_map[codigo] = nombre
                
                if batch_mappings:
                    self.db.add_all(batch_mappings)
                    await self.db.flush()
            
            return segregacion_map
            
        except Exception as e:
            logger.error(f"Error cargando mapeo: {e}")
            return {}
    
    async def _log_resumen_ejecutivo(self, resultado_id: UUID):
        """Log resumen ejecutivo del procesamiento"""
        
        resultado = await self.db.get(ResultadoCamila, resultado_id)
        
        logger.info("="*80)
        logger.info("📊 RESUMEN EJECUTIVO - DASHBOARD")
        logger.info("="*80)
        
        logger.info(f"🎯 PRODUCTIVIDAD:")
        logger.info(f"   - {resultado.productividad_hora} cont/hora")
        logger.info(f"   - {resultado.productividad_meta_pct}% de la meta")
        logger.info(f"   - Estado: {'✅ BUENO' if resultado.productividad_hora >= 75 else '⚠️ BAJO'}")
        
        logger.info(f"\n⚖️ BALANCE DE FLUJOS:")
        logger.info(f"   - Entradas: {resultado.total_entradas}")
        logger.info(f"   - Salidas: {resultado.total_salidas}")
        logger.info(f"   - Balance: {resultado.balance_flujos:+d}")
        logger.info(f"   - Categoría: {resultado.balance_categoria}")
        
        if resultado.precision_global:
            logger.info(f"\n🎯 PRECISIÓN DEL MODELO:")
            logger.info(f"   - Precisión global: {resultado.precision_global}%")
            logger.info(f"   - Score coincidencia: {resultado.score_coincidencia}%")
            logger.info(f"   - Ahorro: {resultado.ahorro_distancia_m}m ({resultado.ahorro_tiempo_min} min)")
        
        logger.info(f"\n🏗️ UTILIZACIÓN:")
        logger.info(f"   - Grúas: {resultado.total_gruas_utilizadas}/12 ({resultado.utilizacion_gruas_pct}%)")
        logger.info(f"   - Bloque más usado: {resultado.bloque_mas_utilizado} ({resultado.concentracion_maxima_pct}%)")
        
        logger.info("="*80)
        
    def obtener_id_grua(self, grua):
        """
        Maneja 'GX' (Antiguo), 'rtgX' (Nuevo), 'rsX' (Nuevo).
        Retorna: (id_numerico, tipo_equipo)
        """
        tipo = 'GENERICO'
        match_grua = re.match(r"([A-Z]+)(\d+)", grua)
        if match_grua:
            prefijo = match_grua.group(1) # 'G', 'RTG', 'RS'
            numero = int(match_grua.group(2)) # 1, 5, etc
        
            if 'RTG' in prefijo:
                tipo = 'RTG'
            elif 'RS' in prefijo:
                tipo = 'RS'
            else:
                tipo = 'GRUA'
        else:
            digits = re.sub(r'\D', '', grua)
            prefijo = int(digits) if digits else 0
        
        return numero, tipo