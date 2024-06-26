# === BUILT-IN
import itertools as itr
from collections import defaultdict
from copy import deepcopy
from functools import reduce
from math import prod
from typing import Dict, Hashable, List

# === THIRD-PARTY
import networkx as nx
import numpy as np
import xgboost as xgb
from einops import repeat, einsum
from pgmpy.factors.discrete.CPD import TabularCPD
from pgmpy.inference import BeliefPropagation, VariableElimination
from pgmpy.models import BayesianNetwork
from scipy.special import logsumexp
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from tqdm import tqdm

from graphical_models.classes.dags.dag import DAG
from graphical_models.classes.dags.functional_dag import FunctionalDAG
# === LOCAL
from graphical_models.utils import core_utils


def no_warn_log(x, eps=1e-10):
    ixs = x > eps
    res = np.log(x, where=ixs)
    res[~ixs] = -np.inf
    return res


def add_repeated_nodes_marginal(
    conditional: np.ndarray, 
    marginal_nodes: list, 
    cond_nodes: list,
    node2dims: dict,
    cond_vals: int
):
    marginal_nodes_no_repeats = [node for node in marginal_nodes if node not in cond_nodes]
    marginal_nodes_repeat = [node for node in marginal_nodes if node in cond_nodes]
    
    # === SET UP PATTERNS FOR START AND END DIMENSION
    start_dims = " ".join([f"d{ix}" for ix in marginal_nodes_no_repeats])
    end_dims = " ".join([
        f"d{node}" if node in marginal_nodes_no_repeats else f"r{node}" 
        for node in marginal_nodes
    ])
    
    # === FOR EACH MARGINAL NODE THAT'S IN THE CONDITIONING SET, REPEAT
    pattern = start_dims + " -> " + end_dims
    repeats = {f"r{node}": node2dims[node] for node in marginal_nodes_repeat}
    conditional = repeat(conditional, pattern, **repeats)

    # NOTE: THE BELOW NO LONGER APPLIES, SINCE WHETHER WE SHOULD SET CONDITIONAL = 0
    # DEPENDS ON WHAT THE CONDITIONING VALUE OF THE NODE IS
    if len(marginal_nodes_repeat) > 1:
        raise NotImplementedError
    else:
        rep_node = marginal_nodes_repeat[0]
        cond_repeat_ix = cond_nodes.index(rep_node)
        ones = (np.arange(node2dims[marginal_nodes_repeat[0]]) == cond_vals[cond_repeat_ix]).astype(int)
        repeats = {f"d{node}": node2dims[node] for node in marginal_nodes if node != rep_node}
        ones = repeat(ones, f"r{rep_node} -> {end_dims}", **repeats)
    conditional = conditional * ones
    
    return conditional


def add_repeated_nodes_conditional(
    conditional: np.ndarray, 
    marginal_nodes: list, 
    cond_nodes: list,
    node2dims: dict
):
    marginal_nodes_no_repeats = [node for node in marginal_nodes if node not in cond_nodes]
    all_nodes = marginal_nodes_no_repeats + cond_nodes
    marginal_nodes_repeat = [node for node in marginal_nodes if node in cond_nodes]
    
    # === SET UP PATTERNS FOR START AND END DIMENSION
    start_dims = " ".join([f"d{ix}" for ix in all_nodes])
    end_dims = " ".join([
        f"d{node}" if node in marginal_nodes_no_repeats else f"r{node}" 
        for node in marginal_nodes
    ])
    end_dims += " " + " ".join([f"d{node}" for node in cond_nodes])
    
    # === FOR EACH MARGINAL NODE THAT'S IN THE CONDITIONING SET, REPEAT
    pattern = start_dims + " -> " + end_dims
    repeats = {f"r{node}": node2dims[node] for node in marginal_nodes_repeat}
    conditional = repeat(conditional, pattern, **repeats)

    ones = [np.eye(node2dims[node]) for node in marginal_nodes_repeat]
    if len(ones) > 1:
        raise NotImplementedError
    else:
        ones = ones[0]
        rep_node = marginal_nodes_repeat[0]
        repeats = {f"d{node}": node2dims[node] for node in all_nodes if node != rep_node}
        ones = repeat(ones, f"d{rep_node} r{rep_node} -> {end_dims}", **repeats)
    conditional = conditional * ones
    
    return conditional


def repeat_dimensions(
    tensor, 
    curr_dims, 
    new_dims, 
    dim_sizes, 
    add_new=True
):
    start_dims = " ".join([f"d{ix}" for ix in curr_dims])
    end_dims = " ".join([f"d{ix}" for ix in new_dims])
    if add_new:
        start_dims += " d_new"
        end_dims += " d_new"
    repeat_pattern = start_dims + " -> " + end_dims
    repeats = {f"d{ix}": dim_sizes[ix] for ix in new_dims if ix not in curr_dims}
    new_tensor = repeat(tensor, repeat_pattern, **repeats)
    return new_tensor


def extract_conditional(model, alphabets, node_alphabet):
    vals = np.array(list(itr.product(*alphabets)))
    probs = model.predict_proba(vals)
    
    nvals = probs.shape[1]
    parent_dims = list(map(len, alphabets))
    conditional = probs.reshape(parent_dims + [nvals])
    
    if nvals != len(node_alphabet):
        conditional2 = np.zeros(parent_dims + [len(node_alphabet)])
        conditional2[..., model.classes_] = conditional
        return conditional2
    
    return conditional


def indicator_conditional(input_alphabets, output_alphabet, value):
    shape = list(map(len, input_alphabets)) + [len(output_alphabet)]
    
    conditional = np.zeros(shape)
    ix = output_alphabet.index(value)
    conditional[..., ix] = 1
    return conditional


def get_conditional(
    data: np.ndarray, 
    node: int, 
    vals, 
    parent_ixs: list, 
    parent_alphabets,
    add_one=False,
    alpha: float = 1
):
    if len(parent_ixs) == 0:
        counts = np.array([np.sum(data[:, node] == val) for val in vals]).astype(float)
        if add_one:
            counts += alpha
        return counts / counts.sum()
    else:
        nvals = len(vals)
        conditional = np.ones(list(map(len, parent_alphabets)) + [nvals]) * 1/nvals
        parent_vals_list = {tuple(parent_vals) for parent_vals in data[:, parent_ixs]}
        for parent_vals in parent_vals_list:
            ixs = (data[:, parent_ixs] == parent_vals).all(axis=1)
            subdata = data[ixs, :]
            if subdata.shape[0] > 0:
                conditional[tuple(parent_vals)] = get_conditional(subdata, node, vals, [], [], add_one=add_one, alpha=alpha)
        return conditional


def add_variable(
    table, 
    current_variables, 
    conditional, 
    node2dims, 
    parents
):
    log_conditional = no_warn_log(conditional)

    log_conditional = repeat_dimensions(
        log_conditional,
        parents,
        current_variables,
        node2dims,
    )
    
    new_table = table.reshape(table.shape + (1, )) + log_conditional
    return new_table


def marginalize(table, ixs):
    return logsumexp(table, axis=tuple(ixs))


class DiscreteDAG(FunctionalDAG):
    def __init__(
        self, 
        nodes, 
        arcs, 
        conditionals: Dict[Hashable, np.ndarray], 
        node2parents: Dict[Hashable, List],
        node_alphabets: Dict[Hashable, List]
    ):
        super().__init__(set(nodes), arcs)
        self.conditionals = conditionals
        self.node2parents = node2parents
        self.node_alphabets = node_alphabets
        self.node2dims = {
            node: len(alphabet) 
            for node, alphabet in self.node_alphabets.items()
        }
        self._node_list = list(nodes)
        self._node2ix = core_utils.ix_map_from_list(self._node_list)
        
        for node, parents in node2parents.items():
            expected_shape = tuple([len(node_alphabets[p]) for p in parents + [node]])
            if conditionals is not None:
                assert conditionals[node].shape == expected_shape
        
    def copy(self):
        return deepcopy(self)

    def set_conditional(self, node, cpt):
        self.conditionals[node] = cpt

    def sample(self, nsamples: int = 1, progress=False) -> np.array:
        samples = np.zeros((nsamples, len(self._nodes)), dtype=int)
        t = self.topological_sort()
        t = t if not progress else tqdm(t)

        for node in t:
            parents = self.node2parents[node]
            if len(parents) == 0:
                vals = np.random.choice(
                    self.node_alphabets[node], 
                    p=self.conditionals[node], 
                    size=nsamples
                )
            else:
                parent_ixs = [self._node2ix[p] for p in parents]
                parent_vals = samples[:, parent_ixs]
                dists = self.conditionals[node][tuple(parent_vals.T)]
                unifs = np.random.random(size=nsamples)
                dist_sums = np.cumsum(dists, axis=1)
                vals = np.argmax(dist_sums > unifs[:, None], axis=1)
            samples[:, self._node2ix[node]] = vals

        return samples
    
    def weighted_samples(self, nodes: list, values: np.ndarray, progress=False):
        nsamples = values.shape[0]
        samples = np.zeros((nsamples, len(self._nodes)), dtype=int)
        samples[:, nodes] = values
        weights = np.zeros((nsamples, len(nodes)))
        
        t = self.topological_sort()
        t = t if not progress else tqdm(t)

        for node in t:
            if node not in nodes:
                parents = self.node2parents[node]
                if len(parents) == 0:
                    vals = np.random.choice(
                        self.node_alphabets[node], 
                        p=self.conditionals[node], 
                        size=nsamples
                    )
                else:
                    parent_ixs = [self._node2ix[p] for p in parents]
                    parent_vals = samples[:, parent_ixs]
                    dists = self.conditionals[node][tuple(parent_vals.T)]
                    unifs = np.random.random(size=nsamples)
                    dist_sums = np.cumsum(dists, axis=1)
                    vals = np.argmax(dist_sums > unifs[:, None], axis=1)
                samples[:, self._node2ix[node]] = vals
            else:
                parents = self.node2parents[node]
                node_values = values[:, nodes.index(node)]
                if len(parents) == 0:
                    dists = self.conditionals[node]
                    probs = dists[node_values]
                else:
                    parent_ixs = [self._node2ix[p] for p in parents]
                    parent_vals = samples[:, parent_ixs]
                    dists = self.conditionals[node][tuple(parent_vals.T)]
                    probs = dists[np.arange(nsamples), node_values]
                weights[:, nodes.index(node)] = probs

        return samples, weights

    def sample_interventional(self, nodes2intervention_values):
        nsamples = list(nodes2intervention_values.values())[0].shape[0]
        samples = np.zeros((nsamples, self.nnodes), dtype=int)
        t = self.topological_sort()

        for node in t:
            parents = self.node2parents[node]
            node_ix = self._node2ix[node]
            if node in nodes2intervention_values:
                samples[:, node_ix] = nodes2intervention_values[node_ix]
            else:
                if len(parents) == 0:
                    vals = np.random.choice(
                        self.node_alphabets[node], 
                        p=self.conditionals[node], 
                        size=nsamples
                    )
                else:
                    parent_ixs = [self._node2ix[p] for p in parents]
                    parent_vals = samples[:, parent_ixs]
                    dists = [self.conditionals[node][tuple(v)] for v in parent_vals]
                    vals = [np.random.choice(self.node_alphabets[node], p=d) for d in dists]
                samples[:, node_ix] = vals

        return samples

    def log_probability(self, samples: np.ndarray):
        raise NotImplementedError
    
    def predict_from_parents(self, node, parent_vals):
        conditional = self.conditionals[node]
        ixs = tuple(parent_vals.T)
        return conditional[ixs]
        
    def get_hard_interventional_dag(self, target_node, value):
        # assert len(self.parents_of(target_node)) == 0
        node_alphabet = self.node_alphabets[target_node]
        target_conditional = np.array([1 if v == value else 0 for v in node_alphabet])
        new_conditionals = {
            node: self.conditionals[node] if node != target_node else target_conditional
            for node in self.nodes  
        }
        new_node2parents = deepcopy(self.node2parents)
        new_node2parents[target_node] = []
        new_arcs = {(i, j) for i, j in self.arcs if j != target_node}
        
        return DiscreteDAG(
            nodes=self.nodes,
            arcs=new_arcs,
            conditionals=new_conditionals,
            node2parents=new_node2parents,
            node_alphabets=self.node_alphabets
        )
        
    def _get_marginal_dag_node(self, marginalized_node, relabel=False):
        # === NEED PARENTS AND CHILDREN OF THIS NODE
        m_children = self.children_of(marginalized_node)
        m_parents = self.node2parents[marginalized_node]
        m_conditional = self.conditionals[marginalized_node]
        m_shape = " ".join([f"d{i}" for i in m_parents + [marginalized_node]])
        
        # === SPECIFY NEW PARENT SETS AND CORRESPONDING ARCS
        new_arcs = {
            (i, j) for i, j in self.arcs 
            if j != marginalized_node and i != marginalized_node
        }
        new_node2parents = deepcopy(self.node2parents)
        del new_node2parents[marginalized_node]
        for m_child in m_children:
            new_parents = [p for p in self.node2parents[m_child] if p != marginalized_node]
            new_parents += [p for p in m_parents if p not in self.node2parents[m_child]]
            new_node2parents[m_child] = new_parents
            
            new_arcs |= {(p, m_child) for p in new_parents}
            
        # === COMPUTE NEW CONDITIONALS
        new_conditionals = {
            node: self.conditionals[node]
            for node in self.nodes - {marginalized_node} - m_children
        }
        for m_child in m_children:
            child_conditional = self.conditionals[m_child]
            
            # === COMPUTE NEW CONDITIONAL FROM OLD
            child_shape = " ".join([f"d{i}" for i in self.node2parents[m_child] + [m_child]])
            output_shape = " ".join([f"d{i}" for i in new_node2parents[m_child] + [m_child]])
            pattern = f"{child_shape}, {m_shape} -> {output_shape}"
            new_conditional = einsum(child_conditional, m_conditional, pattern)
            # the line below is only needed for numerical stability
            new_conditional = new_conditional / new_conditional.sum(axis=-1, keepdims=True)
            new_conditionals[m_child] = new_conditional
            
        new_nodes = self.nodes - {marginalized_node}
        new_alphabets = {k: v for k, v in self.node_alphabets.items() if k != marginalized_node}
        labels = {node: node for node in new_nodes}
        if relabel:
            labels = {node: node if node < marginalized_node else node - 1 for node in new_nodes}
            new_nodes = {labels[node] for node in new_nodes}
            new_arcs = {(labels[i], labels[j]) for i, j in new_arcs}
            new_conditionals = {labels[node]: new_conditionals[node] for node in new_conditionals}
            new_node2parents = {labels[node]: [labels[p] for p in new_node2parents[node]] for node in new_node2parents}
            new_alphabets = {labels[node]: new_alphabets[node] for node in new_alphabets}
        
        new_ddag = DiscreteDAG(
            nodes=new_nodes,
            arcs=new_arcs,
            conditionals=new_conditionals,
            node2parents=new_node2parents,
            node_alphabets=new_alphabets
        )
        
        return new_ddag, labels
        
    def get_marginal_dag(self, marginalized_nodes, relabel=False):
        new_dag = self.copy()
        relabeling_function = {node: node for node in self.nodes}
        nodes2marginalize = list(marginalized_nodes)
        while len(nodes2marginalize) > 0:
            node = nodes2marginalize.pop()
            node_label = relabeling_function[node]
            new_dag, labels = new_dag._get_marginal_dag_node(node_label, relabel=relabel)
            relabeling_function = {
                node: labels[relabeling_function[node]] 
                for node in relabeling_function
                if relabeling_function[node] in labels
            }
        return new_dag, relabeling_function
        
    def get_marginals_new(self, marginal_nodes: List[Hashable], log=False):
        if len(marginal_nodes) == 0:
            return 1
        ancestor_subgraph = self.ancestral_subgraph(set(marginal_nodes))
        elimination_ordering = ancestor_subgraph.topological_sort()
        node0 = elimination_ordering[0]
        current_nodes = [node0]
        log_table = np.log(self.conditionals[node0])
        
        for elim_node in elimination_ordering:
            # === ADD FACTORS INVOLVING THIS NODE
            new_children = list(ancestor_subgraph.children_of(elim_node) - set(current_nodes))
            
            for child in new_children:
                remaining_parents = [p for p in self.node2parents[child] if p in current_nodes]
                log_table = add_variable(
                    log_table,
                    current_nodes,
                    self.conditionals[child],
                    self.node2dims,
                    remaining_parents,
                )
                current_nodes.append(child)
                
            # === ELIMINATE THE CURRENT NODE
            if elim_node not in marginal_nodes:
                ix = current_nodes.index(elim_node)
                log_table = marginalize(log_table, [ix])
                current_nodes = [node for node in current_nodes if node != elim_node]
            
        if not log:
            table = np.exp(log_table)
        else:
            table = log_table
        
        return repeat_dimensions(table, current_nodes, marginal_nodes, None, add_new=False)

    def get_marginals(self, marginal_nodes: List[Hashable], log=False):
        ancestor_subgraph = self.ancestral_subgraph(set(marginal_nodes))
        t = ancestor_subgraph.topological_sort()

        current_nodes = [t[0]]
        added_nodes = {t[0]}
        log_table = np.log(self.conditionals[t[0]])
        
        for new_node in t[1:]:
            node2ix = {node: ix for ix, node in enumerate(current_nodes)}

            log_table = add_variable(
                log_table, 
                current_nodes,
                self.conditionals[new_node], 
                self.node2dims, 
                self.node2parents[new_node]
            )
            current_nodes.append(new_node)
            added_nodes.add(new_node)

            # === MARGINALIZE ANY NODE WHERE ALL CHILDREN HAVE BEEN ADDED
            marginalizable_nodes = {
                node for node in current_nodes 
                if (ancestor_subgraph.children_of(node) <= added_nodes)
                and (node not in marginal_nodes)
            }
            if len(marginalizable_nodes) > 0:
                log_table = marginalize(log_table, [node2ix[node] for node in marginalizable_nodes])
                current_nodes = [
                    node for node in current_nodes
                    if node not in marginalizable_nodes
                ]
        
        if not log:
            table = np.exp(log_table)
        else:
            table = log_table
        return repeat_dimensions(table, current_nodes, marginal_nodes, None, add_new=False)

    def get_marginal(self, node, verbose=False, log=False):
        ancestor_subgraph = self.ancestral_subgraph(node)
        t = ancestor_subgraph.topological_sort()
        if verbose: print(f"Ancestor subgraph: {ancestor_subgraph}")

        current_nodes = [t[0]]
        added_nodes = {t[0]}
        table = no_warn_log(self.conditionals[t[0]])
        
        for new_node in t[1:]:
            node2ix = {node: ix for ix, node in enumerate(current_nodes)}

            if verbose: print(f"====== Adding {new_node} to {current_nodes} ======")
            table = add_variable(
                table, 
                current_nodes,
                self.conditionals[new_node], 
                self.node2dims, 
                self.node2parents[new_node]
            )
            current_nodes.append(new_node)
            added_nodes.add(new_node)

            # === MARGINALIZE ANY NODE WHERE ALL CHILDREN HAVE BEEN ADDED
            marginalizable_nodes = {
                node for node in current_nodes 
                if (ancestor_subgraph.children_of(node) <= added_nodes)
                and (node != new_node)
            }
            if verbose: print(f"Marginalizing {marginalizable_nodes}")
            if len(marginalizable_nodes) > 0:
                table = marginalize(table, [node2ix[node] for node in marginalizable_nodes])
                current_nodes = [
                    node for node in current_nodes
                    if node not in marginalizable_nodes
                ]
            
            if verbose: print(f"Shape: {table.shape}")
                
        return np.exp(table)
    
    def _get_conditional_pgmpy_values(
        self,
        marginal_nodes: list,
        cond_nodes: list,
        cond_values: list,
        method : str = "variable_elimination",
        as_dict: bool = False
    ):
        # === TODO: this does not allow for the same variables in marginal_nodes and cond_nodes
        # === idea: remove all overlaps from marginal_nodes
        # === then, at the end we can do an outer product with an indicator
        marginal_nodes_no_repeats = [node for node in marginal_nodes if node not in cond_nodes]
        
        # === CONVERT TO PGMPY AND SET UP VariableElimination OBJECT
        bn = self.to_pgm()
        if method == "variable_elimination":
            infer = VariableElimination(bn)
        elif method == "belief_propagation":
            infer = BeliefPropagation(bn)
        else:
            raise ValueError()
        
        # === EXTRACT DIMENSIONS AND SET UP CONTAINERS
        if not as_dict:
            marginal_dims = [len(self.node_alphabets[node]) for node in marginal_nodes_no_repeats]
            cond_dims = [len(self.node_alphabets[node]) for node in cond_nodes]
            cond_dim_prod = reduce(lambda x, y: x*y, cond_dims) if len(cond_dims) > 0 else 1
            conditional = np.zeros(marginal_dims + [cond_dim_prod])
        else:
            conditional = dict()
        
        # === GET THE CONDITIONAL FOR EVERY ASSIGNMENT OF THE CONDITIONING NODES
        for ix, vals in enumerate(cond_values):
            factor = infer.query(
                variables=marginal_nodes_no_repeats, 
                evidence=dict(zip(cond_nodes, vals))
            )
            probs = factor.values
            if not as_dict:
                conditional[..., ix] = probs
            else:
                marginal_dims = [len(self.node_alphabets[node]) for node in marginal_nodes_no_repeats]
                if len(marginal_nodes) != len(marginal_nodes_no_repeats):
                    probs = add_repeated_nodes_marginal(probs, marginal_nodes, cond_nodes, self.node2dims, vals)
                conditional[vals] = probs
            
        # === RESHAPE
        if not as_dict:
            conditional = conditional.reshape(marginal_dims + cond_dims)
            if len(marginal_nodes) != len(marginal_nodes_no_repeats):
                conditional = add_repeated_nodes_conditional(conditional, marginal_nodes, cond_nodes, self.node2dims)
        
        return conditional
    
    def get_conditional_pgmpy(
        self, 
        marginal_nodes: list, 
        cond_nodes: list,
        cond_values=None,
        method: str = "variable_elimination"
    ):
        # === GET THE CONDITIONAL FOR EVERY ASSIGNMENT OF THE CONDITIONING NODES
        as_dict = cond_values is not None
        if cond_values is None:
            cond_values = list(itr.product(*(self.node_alphabets[c] for c in cond_nodes)))
        # if not as_dict:
        #     ans1 = self._get_conditional_pgmpy_values(
        #         marginal_nodes,
        #         cond_nodes,
        #         cond_values,
        #         method,
        #         as_dict=True
        #     )
        #     ans2 = self._get_conditional_pgmpy_values(
        #         marginal_nodes,
        #         cond_nodes,
        #         cond_values,
        #         method,
        #         as_dict=False
        #     )
        #     breakpoint()
        return self._get_conditional_pgmpy_values(
            marginal_nodes,
            cond_nodes,
            cond_values,
            method,
            as_dict=as_dict
        )
        
    def get_conditional_importance_sampling(
        self, 
        marginal_nodes: list, 
        cond_nodes: list,
        cond_values=None,
        nparticles: int = 1000
    ):
        # === GET THE CONDITIONAL FOR EVERY ASSIGNMENT OF THE CONDITIONING NODES
        vals = np.repeat(np.array(list(cond_values)), repeats=nparticles, axis=0)
        samples, weights = self.weighted_samples(list(cond_nodes), vals)
        prod_weights = np.prod(weights, axis=1)
        
        conds2condtionals = dict()
        conds2marginals = dict()
        shape = [len(self.node_alphabets[node]) for node in marginal_nodes]
        possible_marginal_vals = list(itr.product(*(self.node_alphabets[node] for node in marginal_nodes)))
        for ix, cond_val in enumerate(cond_values):
            start_ix, end_ix = nparticles * ix, nparticles * (ix + 1)
            subset_weights = prod_weights[start_ix:end_ix]
            subset_samples = samples[start_ix:end_ix, marginal_nodes]
            
            conditional_unnorm = np.zeros(shape)
            for marginal_node_vals in possible_marginal_vals:
                ixs = np.where((subset_samples == marginal_node_vals).all(axis=1))[0]
                conditional_unnorm[marginal_node_vals] = np.sum(subset_weights[ixs])
            conditional = conditional_unnorm / np.sum(conditional_unnorm)
            
            conds2condtionals[cond_val] = conditional
            conds2marginals[cond_val] = np.sum(subset_weights) / np.sum(prod_weights)
            
        return conds2condtionals, conds2marginals

    def get_conditional(self, marginal_nodes, cond_nodes, method="new"):
        marginal_nodes_no_repeats = [node for node in marginal_nodes if node not in cond_nodes]

        # === COMPUTE MARGINAL OVER ALL INVOLVED NODES
        all_nodes = marginal_nodes_no_repeats + cond_nodes
        
        if method == "new":
            full_log_marginal = self.get_marginals_new(all_nodes, log=True)
        else:
            full_log_marginal = self.get_marginals(all_nodes, log=True)

        # === MARGINALIZE TO JUST THE CONDITIONING SET AND RESHAPE
        cond_log_marginal = marginalize(full_log_marginal, list(range(len(marginal_nodes_no_repeats))))
        cond_log_marginal_rs = cond_log_marginal.reshape((1, ) * len(marginal_nodes_no_repeats) + cond_log_marginal.shape)

        # === COMPUTE CONDITIONAL BY SUBTRACTION IN LOG DOMAIN, THEN EXPONENTIATE
        log_conditional = full_log_marginal - cond_log_marginal_rs
        conditional = np.exp(log_conditional)

        # === ACCOUNT FOR DIVISION BY ZERO
        ixs = np.where(cond_log_marginal == -np.inf)
        marginal_alphabet_size = prod((self.node2dims[node] for node in marginal_nodes_no_repeats))
        for ix in zip(*ixs):
            full_index = (slice(None),) * len(marginal_nodes_no_repeats) + ix
            conditional[full_index] = 1/marginal_alphabet_size

        # === ACCOUNT FOR ANY NODES THAT ARE IN BOTH THE MARGINAL AND CONDITIONAL
        if len(marginal_nodes) != len(marginal_nodes_no_repeats):
            marginal_nodes_repeat = [node for node in marginal_nodes if node in cond_nodes]
            start_dims = " ".join([f"d{ix}" for ix in all_nodes])
            end_dims = " ".join([
                f"d{node}" if node in marginal_nodes_no_repeats else f"r{node}" 
                for node in marginal_nodes
            ])
            end_dims += " " + " ".join([f"d{node}" for node in cond_nodes])
            pattern = start_dims + " -> " + end_dims
            repeats = {f"r{node}": self.node2dims[node] for node in marginal_nodes_repeat}
            conditional = repeat(conditional, pattern, **repeats)

            ones = [np.eye(self.node2dims[node]) for node in marginal_nodes_repeat]
            if len(ones) > 1:
                raise NotImplementedError
            else:
                ones = ones[0]
                rep_node = marginal_nodes_repeat[0]
                repeats = {f"d{node}": self.node2dims[node] for node in all_nodes if node != rep_node}
                ones = repeat(ones, f"d{rep_node} r{rep_node} -> {end_dims}", **repeats)
            conditional = conditional * ones
        
        return conditional

    def get_mean_and_variance(self, node):
        alphabet = self.node_alphabets[node]
        marginal = self.get_marginal(node)
        terms = [val * marg for val, marg in zip(alphabet, marginal)]
        mean = sum(terms)
        terms = [(val - mean)**2 * marg for val, marg in zip(alphabet, marginal)]
        variance = sum(terms)
        return mean, variance
    
    def to_torch(self, device=None):
        import torch
        from graphical_models.classes.dags.discrete_dag_torch import DiscreteDAGTorch
        
        if device is None:
            device = torch.device("cuda") if torch.cuda.is_available() else "cpu"
        conditionals = {
            node: torch.from_numpy(conditional.astype(np.float64)).to(device)
            for node, conditional in self.conditionals.items()
        }
        ddag_torch = DiscreteDAGTorch(
            self.nodes,
            self.arcs,
            conditionals,
            self.node2parents,
            self.node_alphabets,
            device=device
        )
        return ddag_torch
    
    def to_pgm(self, as_string=False):
        if as_string:
            nx_graph = nx.DiGraph()
            nx_graph.add_nodes_from([str(node) for node in self._nodes])
            nx_graph.add_edges_from([(str(i), str(j)) for i, j in self._arcs])
        else:
            nx_graph = self.to_nx()
    
        bn = BayesianNetwork(nx_graph)
        for node in self.nodes:
            parents = self.node2parents[node]
            parent_dims = [len(self.node_alphabets[par]) for par in parents]
            card = len(self.node_alphabets[node])
            conditional = self.conditionals[node]
            conditional_rs = conditional.reshape(-1, conditional.shape[-1]).T
            
            node_name = str(node) if as_string else node
            parent_names = [str(p) for p in parents] if as_string else parents
            cpd = TabularCPD(node_name, card, conditional_rs, evidence=parent_names, evidence_card=parent_dims)
            bn.add_cpds(cpd)
            
        return bn

    @classmethod
    def from_pgm(cls, model: BayesianNetwork):
        node_order = list(model.nodes)
        ixs2labels = dict(enumerate(node_order))
        labels2ixs = {label: ix for ix, label in ixs2labels.items()}

        arcs = set()
        conditionals = dict()
        node2parents = dict()
        node_alphabets = dict()
        values2nums = dict()
        for node_ix, node in ixs2labels.items():
            cpd = model.get_cpds(node)

            # === SAVE PARENT ORDER ===
            parents = model.get_parents(node)
            parent_ixs = [labels2ixs[parent] for parent in parents]
            node2parents[node_ix] = parent_ixs

            # === ADD ARCS FROM PARENTS TO NODE ===
            arcs |= {(parent_ix, node_ix) for parent_ix in parent_ixs}

            # === CONVERT CPD SO THAT `node` IS THE LAST DIMENSION ===
            vals = cpd.values
            nparents = len(vals.shape) - 1
            new_dims = list(range(1, nparents+1)) + [0]
            conditionals[node_ix] = vals.transpose(new_dims)
            
            # === SAVE THIS NODE'S ALPHABET ===
            node_alphabets[node_ix] = list(range(vals.shape[0]))

            values2nums[node] = cpd.name_to_no[node]

        dag = DiscreteDAG(
            list(range(len(model.nodes))), 
            arcs=arcs, 
            conditionals=conditionals,
            node2parents=node2parents,
            node_alphabets=node_alphabets
        )
        return dag, node_order, values2nums

    @classmethod
    def fit(
        cls, 
        dag: DAG, 
        data: np.ndarray,
        node2parents=None,
        node_alphabets=None, 
        method="mle", 
        **kwargs
    ):
        methods = {
            "mle", 
            "add_one_mle", 
            "xgboost", 
            "random_forest", 
            "logistic"
        }
        if method not in methods:
            raise NotImplementedError
        
        if node2parents is None:
            node2parents = dict()
            for node in dag.nodes:
                node2parents[node] = list(dag.parents_of(node))
        
        if node_alphabets is None:
            node_alphabets = dict()
            for node in dag.nodes:
                alphabet = list(range(max(data[:, node]) + 1))
                node_alphabets[node] = alphabet
                
        conditionals = dict()
        nodes = dag.topological_sort()
        if method in {"random_forest", "xgboost", "logistic"}:
            for node in nodes:
                parents = node2parents[node]
                alphabet = node_alphabets[node]
                if len(parents) == 0:
                    alpha = kwargs.get("alpha", 1)
                    conditionals[node] = get_conditional(data, node, alphabet, [], [], add_one=False, alpha=alpha)
                else:
                    parent_alphabets = [node_alphabets[p] for p in parents]
                    node_alphabet = node_alphabets[node]
                    if len(set(data[:, node])) == 1:
                        cc = indicator_conditional(parent_alphabets, node_alphabet, data[0, node])
                        conditionals[node] = cc
                    else:
                        if method == "random_forest":
                            model = RandomForestClassifier(**kwargs)
                        elif method == "xgboost":
                            model = xgb.XGBClassifier(predictor="cpu_predictor", **kwargs)
                        elif method == "logistic":
                            penalty = kwargs.get("penalty", "none")
                            model = LogisticRegression(multi_class="multinomial", penalty=penalty)
                        model.fit(data[:, parents], data[:, node])
                        conditionals[node] = extract_conditional(model, parent_alphabets, node_alphabet)
        else:
            add_one = method == "add_one_mle"
            alpha = kwargs.get("alpha", 1)
            for node in nodes:
                parents = node2parents[node]
                alphabet = node_alphabets[node]
                
                if len(parents) == 0:
                    conditionals[node] = get_conditional(data, node, alphabet, [], [], add_one=add_one, alpha=alpha)
                else:
                    parent_alphabets = [node_alphabets[p] for p in parents]
                    conditionals[node] = get_conditional(data, node, alphabet, parents, parent_alphabets, add_one=add_one, alpha=alpha)
            
        return DiscreteDAG(
            dag.nodes,
            dag.arcs,
            conditionals,
            node2parents,
            node_alphabets
        )

    def get_efficient_influence_function_conditionals(
        self, 
        target_ix: int, 
        cond_ix: int, 
        cond_value: int,
        ignored_nodes = set(),
        inference_method="variable_elimination"
    ):
        # ADD TERMS FROM THE EFFICIENT INFLUENCE FUNCTION
        conds2counts = self.get_standard_imset(ignored_nodes=ignored_nodes)
        
        target_values = self.node_alphabets[target_ix]
        indicator = np.array(self.node_alphabets[cond_ix]) == cond_value
        values = np.outer(indicator, target_values)

        conds2means = dict()
        for cond_set in conds2counts:
            if len(cond_set) == 0:
                probs = self.get_marginals([cond_ix, target_ix])
                conds2means[cond_set] = (values * probs).sum()
            else:
                # === COMPUTE CONDITIONAL EXPECTATION
                clist = list(cond_set)
                probs = self.get_conditional_pgmpy([cond_ix, target_ix], clist, method=inference_method)
                values2 = values.reshape(values.shape + (1, ) * len(cond_set))
                exp_val_function = (values2 * probs).sum((0, 1))
                conds2means[cond_set] = exp_val_function
        
        return conds2counts, conds2means
        
    def get_efficient_influence_function_conditionals_partial(
        self, 
        target_ix: int, 
        cond_ix: int, 
        cond_value: int,
        ignored_nodes = set(),
        sampled_values = None,
        inference_method="variable_elimination",
        **kwargs
    ):
        # ADD TERMS FROM THE EFFICIENT INFLUENCE FUNCTION
        conds2counts = self.get_standard_imset(ignored_nodes=ignored_nodes)
        
        target_values = self.node_alphabets[target_ix]
        indicator = np.array(self.node_alphabets[cond_ix]) == cond_value
        values = np.outer(indicator, target_values)

        conds2means = dict()
        for cond_set in conds2counts:
            if len(cond_set) == 0:
                probs = self.get_marginals([cond_ix, target_ix])
                conds2means[cond_set] = (values * probs).sum()
            else:
                # === COMPUTE CONDITIONAL EXPECTATION
                clist = list(cond_set)
                cond_values = {tuple(val) for val in sampled_values[:, cond_set]}
                
                if inference_method == "importance_reweighting":
                    probs, _ = self.get_conditional_importance_sampling(
                        [cond_ix, target_ix],
                        clist,
                        cond_values,
                        nparticles=kwargs["nparticles"]
                    )
                else:
                    probs = self.get_conditional_pgmpy(
                        [cond_ix, target_ix], 
                        clist, 
                        cond_values, 
                        method=inference_method
                    )
                exp_val_function = dict()
                for cond_val, prob in probs.items():
                    expval = (values * prob).sum()
                    exp_val_function[cond_val] = expval
                conds2means[cond_set] = exp_val_function
        
        return conds2counts, conds2means
    
    def get_efficient_influence_function_full(
        self,
        target_ix: int, 
        cond_ix: int, 
        cond_value: int, 
        propensity = None,
        ignored_nodes = set(),
        inference_method="variable_elimination"
    ):
        conds2counts, conds2means = self.get_efficient_influence_function_conditionals_full(
            target_ix,
            cond_ix,
            cond_value,
            ignored_nodes=ignored_nodes,
            inference_method=inference_method
        )
        
        def efficient_influence_function(samples):
            eif_terms = np.zeros((samples.shape[0], len(conds2means)))
            for ix, cond_set in enumerate(conds2means):
                conditional_mean = conds2means[cond_set]
                
                count = conds2counts[cond_set]
                if len(cond_set) == 0:
                    eif_terms[:, ix] = conditional_mean * count
                else:
                    ixs = samples[:, cond_set]
                    eif_terms[:, ix] = conditional_mean[tuple(ixs.T)] * count
            eif = np.sum(eif_terms, axis=1)
            return eif / propensity

        return efficient_influence_function
    
    def get_efficient_influence_function_partial(
        self, 
        target_ix: int, 
        cond_ix: int, 
        cond_value: int, 
        propensity = None,
        ignored_nodes = set(),
        sampled_values = None,
        inference_method="variable_elimination",
        **kwargs
    ):
        conds2counts, conds2means = self.get_efficient_influence_function_conditionals_partial(
            target_ix,
            cond_ix,
            cond_value,
            ignored_nodes=ignored_nodes,
            sampled_values=sampled_values,
            inference_method=inference_method,
            **kwargs
        )
        
        def efficient_influence_function(samples):
            eif_terms = np.zeros((samples.shape[0], len(conds2means)))
            for ix, cond_set in enumerate(conds2means):
                conditional_mean = conds2means[cond_set]
                
                count = conds2counts[cond_set]
                if len(cond_set) == 0:
                    eif_terms[:, ix] = conds2means[cond_set] * count
                else:
                    ixs = samples[:, cond_set]
                    means = np.array([conditional_mean[tuple(ix)] for ix in ixs])
                    eif_terms[:, ix] = means * count
            eif = np.sum(eif_terms, axis=1)
            return eif / propensity

        return efficient_influence_function
    
    def get_efficient_influence_function(
        self, 
        target_ix: int,
        cond_ix: int, 
        cond_value: int, 
        propensity = None,
        ignored_nodes = set(),
        sampled_values = None,
        partial=True,
        inference_method="variable_elimination",
        **kwargs
    ):
        if propensity is None:
            propensity = self.get_marginal(cond_ix)[cond_value]
        
        if partial:
            return self.get_efficient_influence_function_partial(
                target_ix,
                cond_ix,
                cond_value,
                propensity=propensity,
                ignored_nodes=ignored_nodes,
                sampled_values=sampled_values,
                inference_method=inference_method,
                **kwargs
            )
        else:
            return self.get_efficient_influence_function_full(
                target_ix,
                cond_ix,
                cond_value,
                propensity=propensity,
                ignored_nodes=ignored_nodes,
                inference_method=inference_method
            )
            


if __name__ == "__main__":
    conditional0 = 0.5 * np.ones(2)
    conditional1 = np.array([[0.1, 0.9], [0.9, 0.1]])
    conditional2 = np.array([[[0.1, 0.9], [0.9, 0.1]], [[0.8, 0.2], [0.2, 0.8]]])
    ddag = DiscreteDAG(
        [0, 1, 2],
        arcs={(0, 1), (0, 2), (1, 2)},
        conditionals={
            0: conditional0, 
            1: conditional1, 
            2: conditional2
        },
        node_alphabets={0: [0, 1], 1: [0, 1], 2: [0, 1]}
    )
    table = ddag.get_marginal(2)