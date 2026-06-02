# app/models/optimization.py
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, JSON, Text, Numeric, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy import Enum
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from app.models.base import Base

# Importación diferida para resolver referencias circulares de SQLAlchemy
import app.models.camila

class TipoMovimiento(PyEnum):
    YARD = "YARD"
    DLVR = "DLVR"
    LOAD = "LOAD"
    RECV = "RECV"
    DSCH = "DSCH"
    SHFT = "SHFT"

class Escenario(Base):
    __tablename__ = "escenarios"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    anio = Column(Integer, nullable=False, index=True)
    semana = Column(Integer, nullable=False, index=True)
    participacion = Column(Integer, nullable=False, index=True)
    con_dispersion = Column(Boolean, nullable=False, index=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    
    # Relaciones
    instancias = relationship("Instancia", back_populates="escenario", cascade="all, delete-orphan")
    resultados_camila = relationship("ResultadoCamila", back_populates="escenario", cascade="all, delete-orphan")
    movimientos_reales = relationship("MovimientoReal", back_populates="escenario", cascade="all, delete-orphan")
    
    __table_args__ = (
        Index('idx_escenario_unique', 'anio', 'semana', 'participacion', 'con_dispersion', unique=True),
    )

class Instancia(Base):
    __tablename__ = "instancias"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    escenario_id = Column(UUID(as_uuid=True), ForeignKey("escenarios.id", ondelete="CASCADE"), nullable=False)
    codigo = Column(String(50), unique=True, nullable=False)  # ej: "20220103_68_K-pipeline"
    fecha_inicio = Column(DateTime, nullable=False)
    fecha_fin = Column(DateTime, nullable=False)
    anio = Column(Integer, nullable=False, index=True)
    semana = Column(Integer, nullable=False, index=True)
    escenario_nombre = Column(String(100))  # ej: "Participación 68%"
    participacion = Column(Integer, nullable=False, index=True)
    con_dispersion = Column(Boolean, nullable=False, index=True)
    variant = Column(String(50), nullable=False, default="magdalena")
    criterio = Column(Integer, nullable=True) # '2' o '3'
    granularidad = Column(String(20), nullable=True) # 'bahia' o 'pila'
    periodos = Column(Integer, nullable=False, default=21)
    dias = Column(Integer, nullable=False, default=7)
    turnos_por_dia = Column(Integer, nullable=False, default=3)
    estado = Column(String(20), default='completado')
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    fecha_procesamiento = Column(DateTime, nullable=True)
    observaciones = Column(Text)
    total_movimientos = Column(Integer, default=0)
    total_bloques = Column(Integer, default=0)
    total_segregaciones = Column(Integer, default=0)
    
    # Relaciones
    escenario = relationship("Escenario", back_populates="instancias")
    movimientos_modelo = relationship("MovimientoModelo", back_populates="instancia", cascade="all, delete-orphan")
    resultados = relationship("ResultadoGeneral", back_populates="instancia", uselist=False, cascade="all, delete-orphan")
    ocupacion_bloques = relationship("OcupacionBloque", back_populates="instancia", cascade="all, delete-orphan")
    carga_trabajo = relationship("CargaTrabajo", back_populates="instancia", cascade="all, delete-orphan")
    kpis_comparativos = relationship("KPIComparativo", back_populates="instancia", cascade="all, delete-orphan")
    metricas_temporales = relationship("MetricaTemporal", back_populates="instancia", cascade="all, delete-orphan")
    asignaciones_bloques = relationship("AsignacionBloque", back_populates="instancia", cascade="all, delete-orphan")
    
    __table_args__ = (
        Index('idx_optimization_fecha', 'fecha_inicio', 'fecha_fin'),
        Index('idx_optimization_anio_semana', 'anio', 'semana'),
        Index('idx_optimization_participacion', 'participacion', 'con_dispersion'),
    )

class Bloque(Base):
    __tablename__ = "bloques"
    
    id = Column(Integer, primary_key=True)
    codigo = Column(String(10), unique=True, nullable=False)  # C1, C2, etc.
    capacidad_teus = Column(Integer, nullable=False)
    capacidad_bahias = Column(Integer, nullable=False)
    capacidad_original = Column(Integer)  # Nueva: guardar capacidad original
    ubicacion_x = Column(Numeric(10, 2))
    ubicacion_y = Column(Numeric(10, 2))
    activo = Column(Boolean, default=True)
    
    # Relaciones
    ocupaciones = relationship("OcupacionBloque", back_populates="bloque")
    cargas_trabajo = relationship("CargaTrabajo", back_populates="bloque")
    asignaciones = relationship("AsignacionBloque", back_populates="bloque")

class Segregacion(Base):
    __tablename__ = "segregaciones"
    
    id = Column(Integer, primary_key=True)
    codigo = Column(String(50), unique=True, nullable=False)  # S1, S2, etc.
    descripcion = Column(String(200))  # expo-dry-40-EU237
    tipo = Column(String(50))  # expo/impo
    categoria = Column(String(50))  # dry/reefer
    tamano = Column(Integer)  # 20/40
    destino = Column(String(50))
    activo = Column(Boolean, default=True)
    
    # Relaciones
    movimientos_modelo = relationship("MovimientoModelo", back_populates="segregacion")
    asignaciones = relationship("AsignacionBloque", back_populates="segregacion")

# Agregar estos campos al modelo MovimientoReal en app/models/optimization.py

class MovimientoReal(Base):
    __tablename__ = "movimientos_reales"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    escenario_id = Column(UUID(as_uuid=True), ForeignKey("escenarios.id", ondelete="CASCADE"), nullable=False)
    
    # Información del movimiento
    periodo = Column(Integer, nullable=False)
    dia = Column(Integer)
    turno = Column(Integer)
    hora = Column(Integer)
    fecha_hora = Column(DateTime)  # NUEVO: para guardar timestamp completo
    
    segregacion = Column(String(100))  # Segregación original del archivo
    segregacion_mapeada = Column(String(50))  # NUEVO: Código mapeado (S1, S2, etc.)
    tipo_movimiento = Column(Enum(TipoMovimiento), nullable=False)
    bloque_origen = Column(String(100))
    bloque_destino = Column(String(100))
    cantidad = Column(Integer, default=1)
    
    # Campos adicionales
    categoria = Column(String(50))  # NUEVO: categoría del contenedor
    contenedor_id = Column(String(50))  # NUEVO: ID único del contenedor
    
    # Campo para tracking de mapeo
    mapeado = Column(Boolean, default=False)
    segregacion_mapeada_id = Column(Integer, ForeignKey("segregaciones.id"))
    
    # Relaciones
    escenario = relationship("Escenario", back_populates="movimientos_reales")
    segregacion_mapeada_rel = relationship("Segregacion", foreign_keys=[segregacion_mapeada_id])
    
    __table_args__ = (
        Index('idx_movreal_escenario_periodo', 'escenario_id', 'periodo'),
        Index('idx_movreal_segregacion', 'segregacion'),
        Index('idx_movreal_segregacion_mapeada', 'segregacion_mapeada'),
        Index('idx_movreal_tipo', 'tipo_movimiento'),
        Index('idx_movreal_mapeado', 'mapeado'),
    )

class DistanciaReal(Base):
    __tablename__ = "distancias_reales"
    
    id = Column(Integer, primary_key=True)
    origen = Column(String(50), nullable=False)
    destino = Column(String(50), nullable=False)
    distancia_metros = Column(Integer, nullable=False)
    tipo_origen = Column(String(20))  # bloque, gate, sitio
    tipo_destino = Column(String(20))
    
    __table_args__ = (
        Index('idx_distancia_origen_destino', 'origen', 'destino', unique=True),
    )

class ResultadoGeneral(Base):
    __tablename__ = "resultados_generales"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instancia_id = Column(UUID(as_uuid=True), ForeignKey("instancias.id"), nullable=False, unique=True)
    
    # Movimientos detallados
    movimientos_reales_total = Column(Integer, default=0)
    movimientos_yard_real = Column(Integer, default=0)
    movimientos_dlvr_real = Column(Integer, default=0)
    movimientos_load_real = Column(Integer, default=0)
    movimientos_recv_real = Column(Integer, default=0)
    movimientos_dsch_real = Column(Integer, default=0)
    
    # Movimientos modelo
    movimientos_optimizados = Column(Integer, default=0)
    movimientos_dlvr_modelo = Column(Integer, default=0)
    movimientos_load_modelo = Column(Integer, default=0)
    movimientos_reduccion = Column(Integer, default=0)
    movimientos_reduccion_pct = Column(Numeric(5, 2))
    
    # Distancias
    distancia_real_total = Column(Integer, default=0)
    distancia_real_load = Column(Integer, default=0)
    distancia_real_dlvr = Column(Integer, default=0)
    distancia_real_yard = Column(Integer, default=0)
    distancia_modelo_total = Column(Integer, default=0)
    distancia_modelo_load = Column(Integer, default=0)
    distancia_modelo_dlvr = Column(Integer, default=0)
    distancia_reduccion = Column(Integer, default=0)
    distancia_reduccion_pct = Column(Numeric(5, 2))
    
    # Eficiencia
    eficiencia_real = Column(Numeric(5, 2))
    eficiencia_modelo = Column(Numeric(5, 2), default=100)
    eficiencia_ganancia = Column(Numeric(5, 2))
    
    # Segregaciones
    segregaciones_total = Column(Integer, default=0)
    segregaciones_optimizadas = Column(Integer, default=0)
    
    # Carga de trabajo
    carga_trabajo_total = Column(Integer, default=0)
    variacion_carga = Column(Integer, default=0)
    balance_carga = Column(Integer, default=0)
    carga_maxima = Column(Integer, default=0)
    carga_minima = Column(Integer, default=0)
    
    # Ocupación
    ocupacion_promedio_pct = Column(Numeric(5, 2))
    ocupacion_maxima_pct = Column(Numeric(5, 2))
    ocupacion_minima_pct = Column(Numeric(5, 2))
    capacidad_total_teus = Column(Integer, default=0)
    
    # Metadata
    archivo_distancias_usado = Column(String(255))
    fecha_calculo = Column(DateTime, default=datetime.utcnow)
    
    instancia = relationship("Instancia", back_populates="resultados")

class AsignacionBloque(Base):
    __tablename__ = "asignaciones_bloques"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instancia_id = Column(UUID(as_uuid=True), ForeignKey("instancias.id"), nullable=False)
    segregacion_id = Column(Integer, ForeignKey("segregaciones.id"), nullable=False)
    bloque_id = Column(Integer, ForeignKey("bloques.id"), nullable=True)
    total_bloques_asignados = Column(Integer, default=0)
    bloques_codigos = Column(JSON)  # Lista de códigos de bloques asignados
    
    instancia = relationship("Instancia", back_populates="asignaciones_bloques")
    segregacion = relationship("Segregacion", back_populates="asignaciones")
    bloque = relationship("Bloque", back_populates="asignaciones")
    
    __table_args__ = (
        Index('idx_asignacion_instancia_segregacion', 'instancia_id', 'segregacion_id'),
    )

class MovimientoModelo(Base):
    __tablename__ = "movimientos_modelo"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instancia_id = Column(UUID(as_uuid=True), ForeignKey("instancias.id"), nullable=False)
    segregacion_id = Column(Integer, ForeignKey("segregaciones.id"), nullable=False)
    bloque_id = Column(Integer, ForeignKey("bloques.id"), nullable=False)
    periodo = Column(Integer, nullable=False)
    recepcion = Column(Integer, default=0)
    carga = Column(Integer, default=0)
    descarga = Column(Integer, default=0)
    entrega = Column(Integer, default=0)
    volumen_teus = Column(Integer, default=0)
    bahias_ocupadas = Column(Integer, default=0)
    
    instancia = relationship("Instancia", back_populates="movimientos_modelo")
    segregacion = relationship("Segregacion", back_populates="movimientos_modelo")
    bloque = relationship("Bloque")
    
    __table_args__ = (
        Index('idx_movmodelo_instancia_periodo', 'instancia_id', 'periodo'),
        Index('idx_movmodelo_bloque', 'bloque_id'),
    )

class CargaTrabajo(Base):
    __tablename__ = "carga_trabajo"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instancia_id = Column(UUID(as_uuid=True), ForeignKey("instancias.id"), nullable=False)
    bloque_id = Column(Integer, ForeignKey("bloques.id"), nullable=False)
    periodo = Column(Integer, nullable=False)
    carga_trabajo = Column(Integer, default=0)
    carga_maxima = Column(Integer)
    carga_minima = Column(Integer)
    
    instancia = relationship("Instancia", back_populates="carga_trabajo")
    bloque = relationship("Bloque", back_populates="cargas_trabajo")
    
    __table_args__ = (
        Index('idx_carga_instancia_periodo', 'instancia_id', 'periodo'),
        Index('idx_carga_bloque', 'bloque_id'),
    )

class OcupacionBloque(Base):
    __tablename__ = "ocupacion_bloques"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instancia_id = Column(UUID(as_uuid=True), ForeignKey("instancias.id"), nullable=False)
    bloque_id = Column(Integer, ForeignKey("bloques.id"), nullable=False)
    periodo = Column(Integer, nullable=False)
    turno = Column(Integer, nullable=False)
    contenedores_teus = Column(Integer, default=0)
    capacidad_bloque = Column(Integer)  # Nueva: guardar capacidad usada
    porcentaje_ocupacion = Column(Numeric(5, 2))
    estado = Column(String(20))  # activo, inactivo
    
    instancia = relationship("Instancia", back_populates="ocupacion_bloques")
    bloque = relationship("Bloque", back_populates="ocupaciones")
    
    __table_args__ = (
        Index('idx_ocupacion_instancia_periodo', 'instancia_id', 'periodo'),
        Index('idx_ocupacion_bloque', 'bloque_id'),
    )

class KPIComparativo(Base):
    __tablename__ = "kpis_comparativos"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instancia_id = Column(UUID(as_uuid=True), ForeignKey("instancias.id"), nullable=False)
    categoria = Column(String(50), nullable=False)  # eficiencia, distancia, movimientos
    metrica = Column(String(100), nullable=False)
    valor_real = Column(Numeric(15, 2))
    valor_modelo = Column(Numeric(15, 2))
    diferencia = Column(Numeric(15, 2))
    porcentaje_mejora = Column(Numeric(5, 2))
    unidad = Column(String(20))
    
    instancia = relationship("Instancia", back_populates="kpis_comparativos")
    
    __table_args__ = (
        Index('idx_kpi_instancia_categoria', 'instancia_id', 'categoria'),
    )

class MetricaTemporal(Base):
    __tablename__ = "metricas_temporales"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instancia_id = Column(UUID(as_uuid=True), ForeignKey("instancias.id"), nullable=False)
    periodo = Column(Integer)
    dia = Column(Integer)
    turno = Column(Integer)
    movimientos_real = Column(Integer, default=0)
    movimientos_yard_real = Column(Integer, default=0)
    movimientos_modelo = Column(Integer, default=0)
    distancia_real = Column(Integer, default=0)
    distancia_modelo = Column(Integer, default=0)
    carga_trabajo = Column(Integer, default=0)
    ocupacion_promedio = Column(Numeric(5, 2))
    
    instancia = relationship("Instancia", back_populates="metricas_temporales")
    
    __table_args__ = (
        Index('idx_metrica_instancia_tiempo', 'instancia_id', 'dia', 'turno'),
        Index('idx_metrica_periodo', 'periodo'),
    )

class LogProcesamiento(Base):
    __tablename__ = "logs_procesamiento"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instancia_id = Column(UUID(as_uuid=True), ForeignKey("instancias.id"), nullable=False)
    archivo_nombre = Column(String(255))
    archivo_tipo = Column(String(50))  # resultado, flujos, distancias, instancia
    fecha_procesamiento = Column(DateTime, default=datetime.utcnow)
    registros_procesados = Column(Integer, default=0)
    estado = Column(String(20))
    mensaje_error = Column(Text)
    duracion_segundos = Column(Integer)
    
    __table_args__ = (
        Index('idx_log_instancia', 'instancia_id'),
        Index('idx_log_fecha', 'fecha_procesamiento'),
    )