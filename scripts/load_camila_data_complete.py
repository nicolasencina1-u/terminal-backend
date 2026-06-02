# scripts/load_camila_data_complete.py

"""
Ejemplos de uso:  
  # Cargar una semana específica
  python load_camila_data_complete.py --semana 2022-01-03 --participacion 68
  
  # Cargar múltiples semanas
  python load_camila_data_complete.py --semanas 2022-01-03 2022-01-10 2022-01-17 --participacion 68
  
  # Cargar todas las semanas de un año
  python load_camila_data_complete.py --anio 2022 --participacion 68
  
  # Cargar rango de años
  python load_camila_data_complete.py --anios 2021 2022 2023 --participacion 70
"""

import asyncio
import os
from pathlib import Path
import sys
import traceback
from datetime import datetime
import re
import argparse

# Agregar el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import AsyncSessionLocal
from app.services.camila_loader import CamilaLoader


def get_week_from_date(date_str):
    """Obtiene el número de semana ISO desde una fecha YYYY-MM-DD"""
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    return date_obj.isocalendar()[1]


def parse_turno_from_filename(filename):
    """Extrae el número de turno del nombre del archivo"""
    match = re.search(r'_T(\d+)\.xlsx', filename)
    if match:
        return int(match.group(1))
    return None


def get_flujos_filepath(base_path, fecha_str, participacion, magdalena_base_path=None):
    """Busca primero archivo Magdalena, luego flujos raw, y aplica fallback"""
    
    magdalena_path = base_path / 'resultados_magdalena' / fecha_str / f'resultado_{fecha_str}_{participacion}_K.xlsx'
    if magdalena_path.exists():
        print(f"✅ Usando datos Magdalena para validación")
        return str(magdalena_path)
    
    flujos_path = base_path / 'instancias_magdalena' / fecha_str / f'Flujos_w{fecha_str}.xlsx'
    if flujos_path.exists():
        print(f"⚠️ Usando flujos raw")
        return str(flujos_path)
        
    if magdalena_base_path:
        fallback_mag = magdalena_base_path / 'resultados_magdalena' / fecha_str / f'resultado_{fecha_str}_{participacion}_K.xlsx'
        if fallback_mag.exists():
            print(f"✅ Usando datos Magdalena (Fallback) para validación")
            return str(fallback_mag)
            
        fallback_flujos = magdalena_base_path / 'instancias_magdalena' / fecha_str / f'Flujos_w{fecha_str}.xlsx'
        if fallback_flujos.exists():
            print(f"⚠️ Usando flujos raw Magdalena (Fallback)")
            return str(fallback_flujos)
            
    return None


def generar_semanas_year(year):
    """Genera todas las semanas de un año en formato YYYY-MM-DD (lunes)"""
    from datetime import date, timedelta
    
    # Encontrar el primer lunes del año
    d = date(year, 1, 1)
    days_ahead = 0 - d.weekday()  # 0 es lunes
    if days_ahead <= 0:  # Ya es lunes o después
        days_ahead += 7
    d = d + timedelta(days_ahead)
    
    semanas = []
    while d.year == year:
        semanas.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=7)
    
    return semanas


async def procesar_semana(fecha_str, semana, anio, participacion, base_path, archivos_sin_flujos, variant=None, criterio=None, granularidad=None, magdalena_base_path=None, camila_base_path=None):
    """Procesa una semana específica"""
    
    fecha_inicio = datetime.strptime(fecha_str, '%Y-%m-%d')
    resultados_path = base_path / 'resultados_camila'
    instancias_camila_path = base_path / 'instancias_camila'
    
    # Buscar directorio de resultados
    fecha_dir = None
    posibles_dirs = []

    if resultados_path.exists():
        for subfolder in resultados_path.iterdir():
            if subfolder.is_dir():
                posibles_dirs.extend([
                    subfolder / f"resultados_turno_{fecha_str}",
                    subfolder / fecha_str,
                    subfolder
                ])

    posibles_dirs.extend([
        resultados_path / f"resultados_turno_{fecha_str}",
        resultados_path / fecha_str,
        resultados_path
    ])
    
    for dir_path in posibles_dirs:
        if dir_path.exists():
            if list(dir_path.glob(f'resultado*_{fecha_str}*.xlsx')) or list(dir_path.glob(f'*{fecha_str}*.xlsx')):
                fecha_dir = dir_path
                break
    
    if not fecha_dir:
        print(f"❌ No se encontró directorio de resultados para {fecha_str} en {resultados_path}")
        return 0, 0
    
    # Buscar archivo de flujos reales
    flujos_real_filepath = get_flujos_filepath(base_path, fecha_str, participacion, magdalena_base_path)
    if flujos_real_filepath:
        print(f"✅ Flujos reales encontrados")
    else:
        print(f"⚠️  Sin flujos reales")
        archivos_sin_flujos.append(fecha_str)
    
    # Buscar archivos de resultado
    resultado_files = sorted(list(fecha_dir.glob(f'*resultado*_{fecha_str}*_T*.xlsx')))
    if len(resultado_files) == 0:
        resultado_files = sorted(list(fecha_dir.glob('resultado_*_T*.xlsx')))
    
    # Buscar archivos de instancia
    instancia_dir = instancias_camila_path / f"instancias_turno_{fecha_str}"
    instancia_files = []
    posibles_instancia_dirs = []
    
    if instancias_camila_path.exists():
        for subfolder in instancias_camila_path.iterdir():
            if subfolder.is_dir():
                posibles_instancia_dirs.extend([
                    subfolder / f"instancias_turno_{fecha_str}",
                    subfolder / fecha_str,
                    subfolder
                ])
                
    posibles_instancia_dirs.extend([
        instancias_camila_path / f"instancias_turno_{fecha_str}",
        instancias_camila_path / fecha_str,
        instancias_camila_path
    ])
    
    for inst_dir in posibles_instancia_dirs:
        if inst_dir.exists():
            archivos_encontrados = sorted(list(inst_dir.glob('Instancia_*_T*.xlsx')))
            if archivos_encontrados:
                instancia_files = archivos_encontrados
                break

    # FALLBACK para Instancia_*.xlsx de Camila
    if not instancia_files and camila_base_path:
        fallback_inst_dir = camila_base_path / 'instancias_camila' / f"instancias_turno_{fecha_str}"
        if not fallback_inst_dir.exists():
            fallback_inst_dir = camila_base_path / 'instancias_camila' / fecha_str
        if fallback_inst_dir.exists():
            instancia_files = sorted(list(fallback_inst_dir.glob('Instancia_*_T*.xlsx')))
            if instancia_files: print(f"⚠️ Usando Instancia.xlsx de Camila Base (Fallback)")
    
    print(f"   📊 {len(resultado_files)} archivos de resultado encontrados en {fecha_dir.name}")
    print(f"   📋 {len(instancia_files)} archivos de instancia")
    if len(resultado_files) == 0:
        return 0, 0
    
    # Procesar cada turno
    turnos_procesados = set()
    exitosos = 0
    fallidos = 0
    
    for resultado_file in resultado_files:
        turno = parse_turno_from_filename(resultado_file.name)
        if turno is None:
            fallidos += 1
            continue
        
        if turno in turnos_procesados:
            continue
        turnos_procesados.add(turno)
        
        # Extraer participación del archivo
        parts = resultado_file.stem.split('_')
        file_participacion = None
        
        for part in parts:
            if part.isdigit() and 60 <= int(part) <= 80 and len(part) <= 3:
                file_participacion = int(part)
                break
        
        # Verificar que coincida con la participación solicitada
        if file_participacion != participacion:
            continue
        
        # Buscar instancia correspondiente
        instancia_file = None
        for inst in instancia_files:
            if (f"_T{turno:02d}" in inst.name or f"_T{turno}" in inst.name) and f"_{participacion}_" in inst.name:
                instancia_file = inst
                break
        
        print(f"\n   ⏰ Turno {turno:02d} - P{participacion}")
        
        try:
            async with AsyncSessionLocal() as db:
                loader = CamilaLoader(db)
                
                resultado_id = await loader.load_camila_results(
                    resultado_filepath=str(resultado_file),
                    instancia_filepath=str(instancia_file) if instancia_file else None,
                    flujos_real_filepath=flujos_real_filepath,
                    fecha_inicio=fecha_inicio,
                    semana=semana,
                    anio=anio,
                    turno=turno,
                    participacion=participacion,
                    con_dispersion=True,
                    variant=variant,
                    criterio=criterio,
                    granularidad=granularidad
                )
                
                await db.commit()
                print(f"   ✅ Cargado exitosamente (Modelo {'Base' if not variant else variant})")
                exitosos += 1
                
        except Exception as e:
            print(f"   ❌ Error: {str(e)}")
            if os.environ.get("DEBUG"):
                traceback.print_exc()
            fallidos += 1
    
    return exitosos, fallidos


async def load_camila_data():
    """Carga datos de Camila con opciones flexibles"""
    
    parser = argparse.ArgumentParser(
        description="Carga datos de Camila a la base de datos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
  # Cargar año con variante
  python load_camila_data_complete.py --anio 2022 --participacion 68 --variant pipeline

  # Cargar una semana específica
  python load_camila_data_complete.py --semana 2022-01-03 --participacion 68
  
  # Cargar múltiples semanas
  python load_camila_data_complete.py --semanas 2022-01-03 2022-01-10 2022-01-17 --participacion 68
  
  # Cargar todas las semanas de un año
  python load_camila_data_complete.py --anio 2022 --participacion 68
  
  # Cargar rango de años
  python load_camila_data_complete.py --anios 2021 2022 2023 --participacion 70
        """
    )
    
    # Argumentos mutuamente exclusivos
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--semana", type=str, 
                      help="Semana específica en formato YYYY-MM-DD (lunes)")
    group.add_argument("--semanas", nargs="+", type=str,
                      help="Lista de semanas en formato YYYY-MM-DD")
    group.add_argument("--anio", type=int,
                      help="Año completo (carga todas las semanas)")
    group.add_argument("--anios", nargs="+", type=int,
                      help="Lista de años a procesar")
    
    # Otros argumentos
    parser.add_argument("--participacion", type=int, required=True,
                      choices=range(60, 81),
                      help="Porcentaje de participación (60-80)")
    parser.add_argument("--debug", action="store_true",
                      help="Mostrar información de debug")
    parser.add_argument("--variant", type=str, default=None,
                      choices=['magdalena', 'pipeline', 'e-constraint'],
                      help="Variante del modelo a cargar (dejar vacio para modelo antiguo)")
    parser.add_argument("--criterio", type=int, choices=[2, 3])
    parser.add_argument("--granularidad", type=str, choices=['bahia', 'pila'])
    
    args = parser.parse_args()
    
    if args.debug:
        os.environ["DEBUG"] = "1"
    
    # Usar variable de entorno de Docker si existe, sino resolver la ruta local dinámicamente
    env_data_path = os.environ.get('DATA_PATH')
    if env_data_path:
        base_path = Path(env_data_path)
    else:
        base_path = Path(__file__).resolve().parent.parent / 'data'
        
    if not base_path.exists():
        print(f"❌ No se encontró la ruta base: {base_path}")
        return
        
    variantes_validas = ['magdalena', 'pipeline', 'e-constraint']
    criterios_validos = [2, 3]
    granularidades_validas = ['bahia', 'pila']
    
    variantes = [args.variant] if args.variant else variantes_validas
    criterios = [args.criterio] if args.criterio else criterios_validos
    granularidades = [args.granularidad] if args.granularidad else granularidades_validas
    
    configs = []
    for var in variantes:
        if var == 'magdalena':
            if not args.criterio or args.criterio == 2:
                configs.append({
                    'variante': 'magdalena',
                    'criterio': 2,
                    'granularidad': None,
                    'path': base_path / 'camila' / '2022'
                })
        else:
            for gran in granularidades:
                for crit in criterios:
                    crit_roman = 'ii' if crit == 2 else 'iii'
                    folder_path = base_path / 'modelos' / var / f"resultados_generados_{gran}_criterio_{crit_roman}"
                    if folder_path.exists():
                        configs.append({
                            'variante': var,
                            'criterio': crit,
                            'granularidad': gran,
                            'path': folder_path
                        })

    if not configs:
        print("⚠️ No se encontraron carpetas válidas para los parámetros especificados.")
        return
    
    # Rutas Base para los fallbacks
    magdalena_base_path = base_path / 'modelos' / 'magdalena' / '2022' / 'instancias_magdalena'
    camila_base_path = base_path / 'camila' / '2022'
    
    semanas_a_procesar = []
    if args.semana: semanas_a_procesar = [args.semana]
    elif args.semanas: semanas_a_procesar = args.semanas
    elif args.anio: semanas_a_procesar = generar_semanas_year(args.anio)
    elif args.anios:
        for anio in args.anios: semanas_a_procesar.extend(generar_semanas_year(anio))
    
    print(f"🔍 CARGA DE DATOS EJECUTIVA - CAMILA")
    print(f"{'='*80}")
    print(f"📋 Semanas a procesar: {len(semanas_a_procesar)}")
    if len(semanas_a_procesar) <= 10:
        for s in semanas_a_procesar: print(f"   - {s}")
    else:
        print(f"   - Desde: {semanas_a_procesar[0]}")
        print(f"   - Hasta: {semanas_a_procesar[-1]}")
    
    total_archivos = 0
    archivos_exitosos = 0
    archivos_fallidos = 0
    archivos_sin_flujos = []
    semanas_procesadas = []
    semanas_sin_datos = []
    
    for config in configs:
        print(f"\n{'='*80}")
        print(f"🔄 PROCESANDO CAMILA: {config['variante'].upper()} | Crit: {config['criterio']} | Gran: {config['granularidad']}")
        print(f"📂 Ruta: {config['path']}")
        print(f"{'='*80}")
        
        for idx, fecha_str in enumerate(semanas_a_procesar, 1):
            try:
                fecha_inicio = datetime.strptime(fecha_str, '%Y-%m-%d')
                semana = get_week_from_date(fecha_str)
                anio = fecha_inicio.year
                
                print(f"\n📁 [{idx}/{len(semanas_a_procesar)}] Procesando {fecha_str} (Año {anio}, Semana {semana})")
                print(f"{'-'*60}")
                
                exitosos, fallidos = await procesar_semana(
                    fecha_str, semana, anio, args.participacion, 
                    config['path'], archivos_sin_flujos, config['variante'],
                    config['criterio'], config['granularidad'],
                    magdalena_base_path=magdalena_base_path,
                    camila_base_path=camila_base_path
                )
                
                if exitosos > 0:
                    semanas_procesadas.append(fecha_str)
                    archivos_exitosos += exitosos
                else:
                    semanas_sin_datos.append(fecha_str)
                
                archivos_fallidos += fallidos
                total_archivos += exitosos + fallidos
                
            except Exception as e:
                print(f"⚠️ Error procesando {fecha_str}: {str(e)}")
                semanas_sin_datos.append(fecha_str)
                continue
    
    # Resumen final
    print(f"\n{'='*80}")
    print(f"✅ CARGA EJECUTIVA COMPLETA")
    print(f"{'='*80}")
    print(f"📊 RESUMEN:")
    print(f"   - Semanas procesadas: {len(semanas_procesadas)}/{len(semanas_a_procesar)}")
    print(f"   - Turnos exitosos: {archivos_exitosos}")
    print(f"   - Turnos fallidos: {archivos_fallidos}")
    print(f"   - Semanas sin flujos: {len(archivos_sin_flujos)}")
    print(f"   - Semanas sin datos: {len(semanas_sin_datos)}")
    
    if archivos_exitosos > 0:
        print(f"   - Tasa de éxito: {(archivos_exitosos/total_archivos*100):.1f}%")
    
    if semanas_sin_datos and len(semanas_sin_datos) <= 10:
        print(f"\n⚠️ Semanas sin datos:")
        for s in semanas_sin_datos:
            print(f"   - {s}")
    
    # Verificación en base de datos
    print(f"\n📊 VERIFICACIÓN EN BASE DE DATOS:")
    try:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import text
            
            # KPIs principales
            kpis_query = """
                SELECT 
                    COUNT(*) as total_resultados,
                    COUNT(DISTINCT semana) as semanas_unicas,
                    COUNT(DISTINCT turno) as turnos_unicos,
                    AVG(productividad_hora) as productividad_promedio,
                    AVG(precision_global) as precision_promedio,
                    MIN(fecha_inicio) as fecha_mas_antigua,
                    MAX(fecha_inicio) as fecha_mas_reciente
                FROM resultados_camila
                WHERE estado IN ('completado', 'COMPLETADO')
                  AND participacion = :participacion
            """
            result = await db.execute(text(kpis_query), {"participacion": args.participacion})
            kpis = result.fetchone()
            
            if kpis and kpis.total_resultados > 0:
                print(f"   ✅ Resultados en BD: {kpis.total_resultados}")
                print(f"   📅 Semanas únicas: {kpis.semanas_unicas}")
                print(f"   🕐 Turnos únicos: {kpis.turnos_unicos}")
                print(f"   📈 Productividad promedio: {kpis.productividad_promedio:.1f} cont/hora" if kpis.productividad_promedio else "   📈 Productividad promedio: N/A")
                print(f"   🎯 Precisión promedio: {kpis.precision_promedio:.1f}%" if kpis.precision_promedio else "   🎯 Precisión promedio: N/A")
                print(f"   📅 Rango: {kpis.fecha_mas_antigua.date()} a {kpis.fecha_mas_reciente.date()}")
            else:
                print(f"   ⚠️ No hay datos en BD para participación {args.participacion}")
            
            # Top 5 mejores turnos
            top_query = """
                SELECT 
                    codigo,
                    fecha_inicio,
                    turno,
                    productividad_hora,
                    precision_global,
                    total_movimientos_modelo
                FROM resultados_camila
                WHERE estado IN ('completado', 'COMPLETADO')
                  AND participacion = :participacion
                  AND productividad_hora IS NOT NULL
                ORDER BY productividad_hora DESC
                LIMIT 5
            """
            result = await db.execute(text(top_query), {"participacion": args.participacion})
            top_turnos = result.fetchall()
            
            if top_turnos:
                print(f"\n📊 TOP 5 TURNOS POR PRODUCTIVIDAD:")
                for t in top_turnos:
                    print(f"   {t.codigo}: {t.productividad_hora:.1f} cont/hora" + 
                          (f" (Precisión: {t.precision_global:.1f}%)" if t.precision_global else ""))
                    
    except Exception as e:
        print(f"\n⚠️ Error en verificación: {str(e)}")
    
    print(f"\n✨ Proceso completado - {datetime.now()}")


if __name__ == "__main__":
    print(f"🚀 INICIANDO CARGA EJECUTIVA DE DATOS CAMILA")
    print(f"   Timestamp: {datetime.now()}")
    print(f"   Con validación estilo Magdalena")
    print(f"{'='*80}\n")
    
    asyncio.run(load_camila_data())