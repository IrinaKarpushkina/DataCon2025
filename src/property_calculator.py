# src/property_calculator.py
import numpy as np
import logging
import sys
import os
from rdkit import Chem
from rdkit.Chem import Descriptors, Crippen, FilterCatalog, FilterCatalogParams
from rdkit.Chem.RDConfig import RDContribDir
from tqdm.auto import tqdm
from .config import MORGAN_GENERATOR

logger = logging.getLogger(__name__)

# --- Фильтры и оценки ---
# Фильтр PAINS
params = FilterCatalogParams()
params.AddCatalog(FilterCatalogParams.FilterCatalogs.PAINS)
PAINS_CATALOG = FilterCatalog(params)

def calculate_sa_score(mol):
    """Расчет SA Score."""
    try:
        # Полноценный расчет, если доступен sascorer
        sys.path.append(os.path.join(RDContribDir, 'SA_Score'))
        import sascorer
        return sascorer.calculateScore(mol)
    except ImportError:
        # Упрощенная аппроксимация, если sascorer не найден
        logp = Descriptors.MolLogP(mol)
        mw = Descriptors.MolWt(mol)
        rot_bonds = Descriptors.NumRotatableBonds(mol)
        score = 2.5 + 0.1 * (mw / 100) + 0.2 * rot_bonds + 0.3 * abs(logp - 2.5)
        return min(score, 10.0)

def has_pains(mol):
    return PAINS_CATALOG.HasMatch(mol)

def check_valency(mol):
    try:
        Chem.SanitizeMol(mol)
        return True
    except:
        return False

def check_lipinski(mol):
    mw = Descriptors.MolWt(mol)
    logp = Descriptors.MolLogP(mol)
    hbd = Descriptors.NumHDonors(mol)
    hba = Descriptors.NumHAcceptors(mol)
    return mw <= 500 and logp <= 5 and hbd <= 5 and hba <= 10

def check_veber(mol):
    tpsa = Descriptors.TPSA(mol)
    rot_bonds = Descriptors.NumRotatableBonds(mol)
    return tpsa <= 140 and rot_bonds <= 10

def calculate_cns_mpo(mol):
    props = [Descriptors.MolLogP(mol), Descriptors.TPSA(mol), Descriptors.MolWt(mol), Descriptors.NumHDonors(mol)]
    if any(p is None for p in props): return 0.0
    logp, tpsa, mw, hbd = props
    scores = [
        np.exp(-((logp - 2.5)**2) / 2),
        np.exp(-((tpsa - 75)**2) / (2 * 30**2)),
        np.exp(-((mw - 400)**2) / (2 * 100**2)),
        np.exp(-(hbd - 2.5)) if hbd > 2.5 else 1.0
    ]
    return np.mean(scores)

def filter_molecules(smiles):
    """Комплексный фильтр для отбора 'хороших' молекул."""
    try:
        mol = Chem.MolFromSmiles(smiles)
        if not mol: return False
        return (
            Descriptors.qed(mol) > 0.6 and 
            calculate_cns_mpo(mol) > 0.5 and 
            check_lipinski(mol) and 
            check_veber(mol) and 
            not has_pains(mol) and 
            calculate_sa_score(mol) <= 4.5
        )
    except:
        return False

# --- Расчет комплексных свойств и вознаграждения ---
def get_features(mol):
    """Генерирует признаки (отпечатки и дескрипторы) для модели."""
    fp = np.array(MORGAN_GENERATOR.GetFingerprint(mol)).reshape(1, -1)
    desc_list = [
        Descriptors.MolLogP, Descriptors.MolWt, Descriptors.TPSA, 
        Descriptors.NumHAcceptors, Descriptors.NumHDonors, Descriptors.NumRotatableBonds,
        Descriptors.FractionCSP3, Descriptors.RingCount, Descriptors.NumAromaticRings, 
        Descriptors.HeavyAtomCount
    ]
    desc = np.array([func(mol) for func in desc_list]).reshape(1, -1)
    return np.hstack((fp, desc))

def calculate_advanced_properties(smiles, keap1_m, egfr_m, ikkb_m):
    """Рассчитывает полный набор свойств для одной молекулы."""
    try:
        mol = Chem.MolFromSmiles(smiles)
        if not mol or not check_valency(mol) or has_pains(mol):
            return None
    except:
        return None

    result = {
        'SMILES': smiles,
        'QED': Descriptors.qed(mol),
        'CNS_MPO': calculate_cns_mpo(mol),
        'SA_Score': calculate_sa_score(mol),
        'RingCount': Descriptors.RingCount(mol),
        'MolWt': Descriptors.MolWt(mol)
    }
    
    # Расчет BBB Score
    logp = Crippen.MolLogP(mol)
    tpsa = Descriptors.TPSA(mol)
    bbb_score = 1.0 - (0.1 if tpsa > 90 else 0) - (0.1 if Descriptors.NumHDonors(mol) > 5 else 0) - (0.1 if logp > 5 else 0)
    result['BBB_Score'] = bbb_score

    # Предсказания моделей, если они доступны
    if keap1_m and egfr_m and ikkb_m:
        try:
            features = get_features(mol)
            pic50_keap1 = keap1_m.predict(features)[0]
            pic50_egfr = egfr_m.predict(features)[0]
            pic50_ikkb = ikkb_m.predict(features)[0]
            
            result.update({
                'pIC50_KEAP1': pic50_keap1,
                'pIC50_EGFR': pic50_egfr,
                'pIC50_IKKb': pic50_ikkb,
                'Selectivity_Score': pic50_keap1 - max(pic50_egfr, pic50_ikkb)
            })
        except Exception as e:
            logger.warning(f"Ошибка предсказания для SMILES {smiles}: {e}")
            return None # Считаем свойство невалидным при ошибке
            
    return result

def calculate_advanced_properties_parallel(smiles_list, keap1_m, egfr_m, ikkb_m):
    """Параллельный расчет свойств для списка SMILES."""
    results = []
    for smi in tqdm(smiles_list, desc="Расчет свойств молекул"):
        props = calculate_advanced_properties(smi, keap1_m, egfr_m, ikkb_m)
        if props:
            results.append(props)
    return results

def _sigmoid(x, k=1.0, x0=0.0):
    if x is None: return 0.0
    return 1 / (1 + np.exp(-k * (x - x0)))

def calculate_multi_objective_reward(props):
    """Расчет многоцелевой функции вознаграждения."""
    if not props: return 0.01

    weights = { 'r_pic50': 0.35, 'r_sel': 0.20, 'r_qed': 0.15, 'r_cns': 0.10, 'r_bbb': 0.10, 'r_sa': 0.10 }
    
    r_pic50 = _sigmoid(props.get('pIC50_KEAP1', 0.0), k=2.0, x0=7.5)
    r_sel = _sigmoid(props.get('Selectivity_Score', 0.0), k=1.5, x0=2.5)
    r_qed = _sigmoid(props.get('QED', 0.0), k=10, x0=0.6)
    r_cns = _sigmoid(props.get('CNS_MPO', 0.0), k=15, x0=0.5)
    r_bbb = _sigmoid(props.get('BBB_Score', 0.0), k=10, x0=0.7)
    r_sa = _sigmoid(-props.get('SA_Score', 10.0), k=1.0, x0=-3.5) # Минимизируем SA Score

    # Если предикторы не использовались, перераспределяем их вес
    if 'pIC50_KEAP1' not in props:
        redist_weight = weights['r_pic50'] + weights['r_sel']
        weights['r_pic50'] = 0
        weights['r_sel'] = 0
        weights['r_qed'] += redist_weight * 0.5
        weights['r_sa'] += redist_weight * 0.5

    total_weight = sum(weights.values())
    if total_weight == 0: return 0.01

    score = (
        weights['r_pic50'] * r_pic50 + weights['r_sel'] * r_sel +
        weights['r_qed'] * r_qed + weights['r_cns'] * r_cns +
        weights['r_bbb'] * r_bbb + weights['r_sa'] * r_sa
    ) / total_weight
    
    return np.clip(score, 0, 1)
