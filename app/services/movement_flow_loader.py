# app/services/movement_flow_loader.py
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from datetime import datetime
import logging
import re
from app.models.movement_flow import MovementFlow

logger = logging.getLogger(__name__)

class MovementFlowLoaderService:
    def __init__(self, db: AsyncSession):
        self.db = db
    
    def extract_patio_bloque(self, position: str) -> tuple[str, str]:
        """
        Extrae patio y bloque de una posición
        Maneja formatos como C3, H5, T2
        """
        if not position or pd.isna(position):
            return None, None
        
        position = str(position).strip().upper()
        
        # Ignorar posiciones especiales
        if position in ['GATE', 'Y-SAI-RAMP', 'Y-SAI-M10', 'VESSEL']:
            return None, None
        
        # Formato simple: C3, H5, T2
        if len(position) == 2 and position[0] in ['C', 'H', 'T'] and position[1].isdigit():
            letra = position[0]
            if letra == 'C':
                return 'costanera', position
            elif letra == 'H':
                return 'ohiggins', position
            elif letra == 'T':
                return 'tebas', position
        
        # Formato Y-SAI-XXX
        if position.startswith('Y-SAI-') and len(position) > 6:
            codigo = position[6:]
            if len(codigo) >= 2 and codigo[0] in ['C', 'H', 'T'] and codigo[1].isdigit():
                letra = codigo[0]
                digito = codigo[1]
                bloque = f"{letra}{digito}"
                
                if letra == 'C':
                    return 'costanera', bloque
                elif letra == 'H':
                    return 'ohiggins', bloque
                elif letra == 'T':
                    return 'tebas', bloque
        
        return None, None
    
    async def load_movement_flows_csv(self, file_path: str, year_from: int = 2017, year_to: int = None):
        """
        Cargar CSV de flujos de movimiento usando chunks para optimizar memoria
        """
        import os
        file_size = os.path.getsize(file_path) / (1024 * 1024)
        logger.info(f"Cargando archivo de flujos: {file_path} ({file_size:.2f} MB)")
        logger.info(f"Filtrando datos desde {year_from} hasta {year_to or 'el último año'}")
        
        chunk_size = 20000
        total_inserted = 0
        total_rows = 0
        
        try:
            with pd.read_csv(file_path, sep=';', chunksize=chunk_size, low_memory=False) as reader:
                logger.info(f"Iniciando lectura de {file_path} en chunks de {chunk_size}...")
                
                import time
                start_load = time.time()

                for i, df in enumerate(reader):
                    chunk_start = time.time()
                    # Limpiar nombres de columnas
                    df.columns = [str(col).strip().lower() for col in df.columns]
                    
                    # Convertir fechas
                    df['ime_time'] = pd.to_datetime(df['ime_time'], errors='coerce')
                    df = df[df['ime_time'].notna()]
                    
                    # Filtrar por año
                    mask = (df['ime_time'].dt.year >= year_from)
                    if year_to:
                        mask &= (df['ime_time'].dt.year <= year_to)
                    df = df[mask]
                    
                    if df.empty:
                        total_rows += chunk_size
                        continue
                    
                    # Convertir tipos
                    if 'ime_ufv_gkey' in df.columns:
                        df['ime_ufv_gkey'] = pd.to_numeric(df['ime_ufv_gkey'], errors='coerce')
                    
                    # Booleanos
                    if 'ig_hazardous' in df.columns:
                        df['ig_hazardous'] = df['ig_hazardous'].fillna('0').astype(str).str.strip() == '1'
                    if 'iu_requires_power' in df.columns:
                        df['iu_requires_power'] = df['iu_requires_power'].fillna('0').astype(str).str.strip() == '1'
                    
                    # Filtrar nulos críticos
                    df = df.dropna(subset=['ime_time', 'ime_ufv_gkey'])
                    
                    if df.empty:
                        total_rows += chunk_size
                        continue
                    
                    logger.info(f"   ⏳ Procesando {len(df)} filas del chunk {i+1}...")
                        
                    records = []
                    # Optimización: Extraer patio y bloque una vez por fila
                    for _, row in df.iterrows():
                        try:
                            # Extraer patio y bloque
                            patio, bloque = self.extract_patio_bloque(row.get('ime_fm'))
                            if not patio:
                                patio, bloque = self.extract_patio_bloque(row.get('ime_to'))
                            
                            records.append({
                                'ime_time': row['ime_time'],
                                'ime_fm': str(row.get('ime_fm', ''))[:50] if pd.notna(row.get('ime_fm')) else None,
                                'ime_to': str(row.get('ime_to', ''))[:50] if pd.notna(row.get('ime_to')) else None,
                                'ime_ufv_gkey': int(row['ime_ufv_gkey']),
                                'ime_move_kind': str(row.get('ime_move_kind', ''))[:50] if pd.notna(row.get('ime_move_kind')) else None,
                                'criterio_i': str(row.get('criterio_i', ''))[:100] if pd.notna(row.get('criterio_i')) else None,
                                'criterio_ii': str(row.get('criterio_ii', ''))[:100] if pd.notna(row.get('criterio_ii')) else None,
                                'criterio_iii': str(row.get('criterio_iii', ''))[:100] if pd.notna(row.get('criterio_iii')) else None,
                                'iu_category': str(row.get('iu_category', ''))[:10],
                                'ig_hazardous': row.get('ig_hazardous', False),
                                'iu_requires_power': row.get('iu_requires_power', False),
                                'iu_freight_kind': str(row.get('iu_freight_kind', ''))[:10],
                                'ret_nominal_length': str(row.get('ret_nominal_length', ''))[:10],
                                'ibcv_id': str(row.get('ibcv_id', ''))[:50],
                                'obcv_id': str(row.get('obcv_id', ''))[:50],
                                'pod1_id': str(row.get('pod1_id', ''))[:10],
                                'patio': patio,
                                'bloque': bloque
                            })
                        except:
                            continue
                    
                    if records:
                        try:
                            stmt = insert(MovementFlow).values(records)
                            await self.db.execute(stmt)
                            await self.db.commit()
                            total_inserted += len(records)
                        except Exception as e:
                            await self.db.rollback()
                            logger.warning(f"Error insertando chunk, intentando uno por uno: {e}")
                            for r in records:
                                try:
                                    await self.db.execute(insert(MovementFlow).values(r))
                                    await self.db.commit()
                                    total_inserted += 1
                                except:
                                    await self.db.rollback()
                                    continue
                    
                    total_rows += len(df)
                    chunk_elapsed = time.time() - chunk_start
                    total_elapsed = time.time() - start_load
                    
                    # Log y Print para máxima visibilidad en el contenedor
                    msg = f"   📊 Chunk {i+1} procesado: {len(records)} registros cargados ({total_inserted} total) en {chunk_elapsed:.2f}s. Total tiempo: {total_elapsed/60:.2f} min"
                    logger.info(msg)
                    print(msg, flush=True)

            
            logger.info(f"✅ Carga de flujos completada: {total_inserted} registros insertados")
            await self.show_statistics()
            return total_inserted
            
        except Exception as e:
            logger.error(f"Error cargando flujos: {e}")
            await self.db.rollback()
            raise

    async def show_statistics(self):
        """Mostrar estadísticas de los datos cargados"""
        from sqlalchemy import select, func, extract
        
        # Total por año
        result = await self.db.execute(
            select(
                extract('year', MovementFlow.ime_time).label('year'),
                func.count(MovementFlow.id).label('total')
            ).group_by('year')
            .order_by('year')
        )
        
        logger.info("\n=== ESTADÍSTICAS POR AÑO ===")
        for row in result:
            logger.info(f"{int(row.year)}: {row.total:,} movimientos")
        
        # Total por tipo de movimiento
        result = await self.db.execute(
            select(
                MovementFlow.ime_move_kind,
                func.count(MovementFlow.id).label('total')
            ).group_by(MovementFlow.ime_move_kind)
            .order_by(func.count(MovementFlow.id).desc())
            .limit(10)
        )
        
        logger.info("\n=== TOP 10 TIPOS DE MOVIMIENTO ===")
        for row in result:
            logger.info(f"{row.ime_move_kind}: {row.total:,} movimientos")
        
        # Total por patio/bloque
        result = await self.db.execute(
            select(
                MovementFlow.patio,
                func.count(MovementFlow.bloque).label('bloques'),
                func.count(MovementFlow.id).label('total')
            ).where(MovementFlow.patio.isnot(None))
            .group_by(MovementFlow.patio)
            .order_by(MovementFlow.patio)
        )
        
        logger.info("\n=== MOVIMIENTOS POR PATIO ===")
        for row in result:
            logger.info(f"{row.patio}: {row.total:,} movimientos")