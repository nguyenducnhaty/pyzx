# PyZX - Python library for quantum circuit rewriting 
#        and optimisation using the ZX-calculus
# Copyright (C) 2018 - Aleks Kissinger and John van de Wetering

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import print_function

__all__ = ['streaming_extract', 'modified_extract']

from fractions import Fraction
import itertools

from .linalg import Mat2, greedy_reduction, column_optimal_swap
from .graph import Graph, EdgeType, VertexType, toggle_edge
from .simplify import id_simp, tcount
from .rules import apply_rule, pivot, match_spider_parallel, spider
from .circuit import Circuit
from .circuit.gates import ParityPhase, CNOT, HAD, ZPhase, CZ, InitAncilla


def bi_adj(g, vs, ws):
    return Mat2([[1 if g.connected(v,w) else 0 for v in vs] for w in ws])

def cut_rank(g, left, right):
    return bi_adj(g, left, right).rank()

def cut_edges(g, left, right, available=None):
    m = bi_adj(g, left, right)
    max_r = max(g.row(v) for v in left)
    for v in g.vertices():
        r = g.row(v)
        if (r > max_r):
            g.set_row(v, r+2)
    x,y = m.factor()

    for v1 in left:
        for v2 in right:
            if (g.connected(v1,v2)):
                g.remove_edge(g.edge(v1,v2))
    
    cut_rank = y.rows()

    #g.add_vertices(2*cut_rank)
    left_verts = []
    right_verts = []
    
    if available == None:
        qs = range(cut_rank)
    else:
        qs = available

    for i in qs:
        v1 = g.add_vertex(VertexType.Z,i,max_r+1)
        v2 = g.add_vertex(VertexType.Z,i,max_r+2)
        #v = vi+cut_rank+i
        #g.add_edge((vi+i,v))
        g.add_edge((v1,v2),EdgeType.HADAMARD)
        left_verts.append(v1)
        right_verts.append(v2)
        #g.set_edge_type(g.edge(vi+i,v), EdgeType.HADAMARD)

    for i in range(y.rows()):
        for j in range(y.cols()):
            if (y.data[i][j]):
                g.add_edge((left[j],left_verts[i]),EdgeType.HADAMARD)
                #g.add_edge((left[j], vi + i))
                #g.set_edge_type(g.edge(left[j], vi + i), EdgeType.HADAMARD)
    for i in range(x.rows()):
        for j in range(x.cols()):
            if (x.data[i][j]):
                g.add_edge((right_verts[j],right[i]),EdgeType.HADAMARD)
                #g.add_edge((vi + cut_rank + j, right[i]))
                #g.set_edge_type(g.edge(vi + cut_rank + j, right[i]), EdgeType.HADAMARD)
    return left_verts


def unspider_by_row(g, v):
    r = g.row(v)
    w = g.add_vertex(VertexType.Z,g.qubit(v),r-1)
    for n in list(g.neighbours(v)):
        if g.row(n) < r:
            e = g.edge(n,v)
            g.add_edge((n,w), edgetype=g.edge_type(e))
            g.remove_edge(e)
    g.add_edge((w, v))
    return w

def connectivity_from_biadj(g, m, left, right, edgetype=EdgeType.HADAMARD):
    for i in range(len(right)):
        for j in range(len(left)):
            if m.data[i][j] and not g.connected(right[i],left[j]):
                g.add_edge((right[i],left[j]),edgetype)
            elif not m.data[i][j] and g.connected(right[i],left[j]):
                g.remove_edge((right[i],left[j]))

def streaming_extract(g, allow_ancillae=False, quiet=True, stopcount=-1):
    """Given a graph put into semi-normal form by :func:`pyzx.simplify.full_reduce`, 
    extracts an equivalent :class:`~pyzx.circuit.Circuit`. 
    Uses a version of the algorithm in `this paper <https://arxiv.org/abs/2003.01664>`__.
    For large graphs, this function can take a couple of minutes to finish.

    Args:
        g: The graph from which a circuit is to be extracted.
        allow_ancillae: Experimental feature to allow extraction for more types of diagrams. Results in post-selected circuits.
        quiet: Whether to print some progress indicators.
        stopcount: If set to a positive integer, stops the extraction after this many gates have been extracted. Useful for debugging or stopping the process when it takes too long.

    Note:
        The graph ``g`` is modified in-place during extraction. If you wish to preserve it, call this function with a copy of it: ``streaming_extract(g.copy())``.
    """
    g.normalise()
    qs = g.qubits() # We are assuming that these are objects that update...
    rs = g.rows()   # ...to reflect changes to the graph, so that when...
    ty = g.types()  # ... g.set_row/g.set_qubit is called, these things update directly to reflect that
    phases = g.phases()
    c = Circuit(g.qubit_count())
    leftrow = 1
    maxq = max(qs.values()) + 1

    nodestotal = tcount(g)
    nodesparsed = 0
    nodesmarker = 10

    # special_nodes contains the ParityPhase like nodes
    special_nodes = {}
    for v in g.vertices():
        if len(list(g.neighbours(v))) == 1 and v not in g.inputs and v not in g.outputs:
            n = list(g.neighbours(v))[0]
            special_nodes[n] = v
        if rs[v] > 1:
            g.set_row(v, rs[v]+20)
    
    tried_id_simp = False
    while True:
        left = [v for v in g.vertices() if rs[v] == leftrow]
        boundary_verts = []
        right = set()
        good_verts = []
        good_neighs = []
        postselects = []
        for v in left:
            # First we add the gates to the circuit that can be processed now,
            # and we simplify the graph to represent this.
            q = qs[v]
            phase = phases[v]
            t = ty[v]
            neigh = [w for w in g.neighbours(v) if rs[w]<leftrow]
            if len(neigh) != 1:
                raise TypeError("Graph doesn't seem circuit like: multiple parents")
            n = neigh[0]
            if qs[n] != q:
                raise TypeError("Graph doesn't seem circuit like: cross qubit connections")
            if g.edge_type(g.edge(n,v)) == EdgeType.HADAMARD:
                c.add_gate("HAD", q)
                g.set_edge_type(g.edge(n,v),EdgeType.SIMPLE)
            if t == VertexType.BOUNDARY: continue # it is an output
            if phase != 0:
                if phase.denominator > 2: nodesparsed += 1
                if t == VertexType.Z: c.add_gate("ZPhase", q, phase=phase)
                else: c.add_gate("XPhase", q, phase=phase)
                g.set_phase(v, 0)
        for v in left:
            q = qs[v]
            t = ty[v]
            neigh = [w for w in g.neighbours(v) if rs[w]==leftrow and w<v]
            for n in neigh:
                t2 = ty[n]
                q2 = qs[n]
                if t == t2:
                    if g.edge_type(g.edge(v,n)) != EdgeType.HADAMARD:
                        raise TypeError("Invalid vertical connection between vertices of the same type")
                    if t == VertexType.Z: c.add_gate("CZ", q2, q)
                    else: c.add_gate("CX", q2, q)
                else:
                    if g.edge_type(g.edge(v,n)) != EdgeType.SIMPLE:
                        raise TypeError("Invalid vertical connection between vertices of different type")
                    if t == VertexType.Z: c.add_gate("CNOT", q, q2)
                    else: c.add_gate("CNOT", q2, q)
                g.remove_edge(g.edge(v,n))
            
            # Done processing gates, now we look to see if we can shift the frontier
            d = [w for w in g.neighbours(v) if rs[w]>leftrow]
            right.update(d)
            if len(d) == 0: 
                if not allow_ancillae: raise TypeError("Not circuit like")
                else:
                    postselects.append(v)
            if len(d) == 1: # Only connected to one node in its future
                if ty[d[0]] != VertexType.BOUNDARY: # which is not an output
                    good_verts.append(v) # So we can make progress
                    good_neighs.append(d[0])
                else:  # This node is done processing, since it is directly (and only) connected to an output
                    boundary_verts.append(v)
                    right.remove(d[0])
        for v in postselects:
            if not quiet: print("postselect", v, qs[v])
            c.add_gate("PostSelect", qs[v])
            left.remove(v)
            g.set_row(v, leftrow-0.5)
            if qs[v] == maxq - 1:
                maxq = maxq -1
        if not good_verts:  # There are no 'easy' nodes we can use to progress
            if all(ty[v] == VertexType.BOUNDARY for v in right): break # Actually we are done, since only outputs are left
            for v in boundary_verts: left.remove(v) # We don't care about the nodes only connected to outputs
            have_removed_gadgets = False
            for n in right.intersection(special_nodes): # Neighbours that are phase gadgets
                targets = set(g.neighbours(n))
                targets.remove(special_nodes[n])
                if targets.issubset(left): # Only connectivity on the lefthandside, so we can extract it
                    nphase = phases[n]
                    if nphase not in (0,1):
                        raise Exception("Can't parse ParityPhase with non-Pauli Phase")
                    phase = phases[special_nodes[n]]
                    c.add_gate("ParityPhase", phase*(-1 if nphase else 1), *[qs[t] for t in targets])
                    g.remove_vertices([special_nodes[n],n])
                    nodesparsed += 1
                    right.remove(n)
                    del special_nodes[n]
                    have_removed_gadgets = True
            if stopcount != -1 and len(c.gates) > stopcount: return c
            if have_removed_gadgets: continue
            right = list(right)
            m = bi_adj(g,right,left)
            m2 = m.copy()
            m2.gauss(full_reduce=True)
            if not any(sum(l)==1 for l in m2.data):
                if not tried_id_simp:
                    tried_id_simp = True
                    i = id_simp(g, matchf=lambda v: rs[v]>leftrow, quiet=True)
                    if i: 
                        if not quiet: print("id_simp found some matches")
                        m = match_spider_parallel(g, matchf=lambda e: rs[g.edge_s(e)]>=leftrow and rs[g.edge_t(e)]>=leftrow)
                        m = [(v1,v2) if v1 in left else (v2,v1) for v1,v2 in m]
                        if not quiet and m: print("spider fusion found some matches")
                        etab, rem_verts, not_needed1, not_needed2 = spider(g, m)
                        g.add_edge_table(etab)
                        g.remove_vertices(rem_verts)
                        continue
                try:
                    gates, lr = handle_phase_gadget(g, left, set(right), special_nodes, quiet=quiet)
                except ValueError:
                    if not allow_ancillae:
                        raise
                    raise Exception
                    gates, maxq = find_ancilla_qubits(g, left, set(right), special_nodes, maxq, quiet=quiet)
                    c.gates.extend(gates)
                    continue
                c.gates.extend(gates)
                nodesparsed += 1
                tried_id_simp = False
                if lr > leftrow:
                    for v in boundary_verts:
                        g.set_row(v, lr)
                    leftrow = lr
                continue
            sequence = greedy_reduction(m) # Find the optimal set of CNOTs we can apply to get a frontier we can work with
            if not isinstance(sequence, list): # Couldn't find any reduction, hopefully we can fix this
                right = set(right)
                gates, success = try_greedy_cut(g, left, right, right.difference(special_nodes), quiet=quiet)
                if success:
                    c.gates.extend(gates)
                    continue
                raise Exception("We should never get here")
                
            if not quiet: print("Greedy reduction with {:d} CNOTs".format(len(sequence)))
            for control, target in sequence:
                c.add_gate("CNOT", qs[left[target]], qs[left[control]])
                # If a control is connected to an output, we need to add a new node.
                for v in g.neighbours(left[control]):
                    if v in g.outputs:
                        #print("Adding node before output")
                        q = qs[v]
                        r = rs[v]
                        w = g.add_vertex(VertexType.Z,q,r-1)
                        e = g.edge(left[control],v)
                        et = g.edge_type(e)
                        g.remove_edge(e)
                        g.add_edge((left[control],w),EdgeType.HADAMARD)
                        g.add_edge((w,v),toggle_edge(et))
                        k = right.index(v)
                        right[k] = w
                        break
                for k in range(len(m.data[control])): # We update the graph to represent the extraction of a CNOT
                    if not m.data[control][k]: continue
                    if m.data[target][k]: g.remove_edge((left[target],right[k]))
                    else: g.add_edge((left[target],right[k]), EdgeType.HADAMARD)
                m.row_add(control, target)
            for v in left:
                d = [w for w in g.neighbours(v) if rs[w]>leftrow]
                if len(d) == 1 and ty[d[0]] != VertexType.BOUNDARY:
                    good_verts.append(v)
                    good_neighs.append(d[0])
            if not good_verts: continue
        
        for v in g.vertices():
            if rs[v] < leftrow: continue
            if v in good_verts: continue
            g.set_row(v,rs[v]+1) # Push the frontier one layer up
        for i,v in enumerate(good_neighs): 
            g.set_row(v,leftrow+1) # Bring the new nodes of the frontier to the correct position
            g.set_qubit(v,qs[good_verts[i]])

        tried_id_simp = False

        if not quiet and nodesparsed > nodesmarker:
            print("{:d}/{:d}".format(nodesparsed, nodestotal))
            nodesmarker = int(round(nodesparsed-5,-1))
            nodesmarker += 10
        leftrow += 1
        if stopcount != -1 and len(c.gates) > stopcount: return c
            
    swap_map = {}
    leftover_swaps = False
    for v in left: # Finally, check for the last layer of Hadamards, and see if swap gates need to be applied.
        q = qs[v]
        neigh = [w for w in g.neighbours(v) if rs[w]>leftrow]
        if len(neigh) != 1: 
            raise TypeError("Algorithm failed: Not fully reducable")
            return c
        n = neigh[0]
        if ty[n] != VertexType.BOUNDARY: 
            raise TypeError("Algorithm failed: Not fully reducable")
            return c
        if g.edge_type(g.edge(n,v)) == EdgeType.HADAMARD:
            c.add_gate("HAD", q)
            g.set_edge_type(g.edge(n,v),EdgeType.SIMPLE)
        if qs[n] != q: leftover_swaps = True
        swap_map[q] = qs[n]
    if leftover_swaps: 
        for t1, t2 in permutation_as_swaps(swap_map):
            c.add_gate("SWAP", t1, t2)
    return c


def try_greedy_cut(g, left, right, candidates, quiet=True):
    q = len(left)
    left = list(left)
    # Take care nothing is connected directly to an output
    for w in right.copy():
        if w in g.outputs:
            w2 = g.add_vertex(VertexType.Z, g.qubit(w), g.row(w)-1)
            n = list(g.neighbours(w))[0] # Outputs should have unique neighbours
            e = g.edge(n,w)
            et = g.edge_type(e)
            g.remove_edge(e)
            g.add_edge((n,w2),EdgeType.HADAMARD)
            g.add_edge((w2,w),toggle_edge(et))
            right.remove(w)
            right.add(w2)
            if w in candidates:
                candidates.remove(w)
                candidates.add(w2)

    right = list(right)
    # We want to figure out which vertices in candidates are 'pivotable'
    # That is, that removing them will decrease the cut rank of the remainder
    m = bi_adj(g, right, left)
    m.gauss(full_reduce=True) # Gaussian elimination doesn't change this property
    good_nodes = []
    for r in m.data:
        if sum(r) == 1: # Exactly one nonzero value, so removing the column with the nonzero value...
            i = next(i for i in range(len(r)) if r[i]) # ...decreases the rank of the matrix
            w = right[i]
            if w in candidates:
                good_nodes.append(w)
    if not good_nodes:
        return [], False
    right = [w for w in right if w not in good_nodes]

    new_right = cut_edges(g, left, right)
    leftrow = g.row(left[0])
    for w in good_nodes: 
        g.set_row(w, leftrow+2)
        new_right.append(unspider_by_row(g, w))

    left.sort(key=g.qubit)
    qs = [g.qubit(v) for v in left]
    m = bi_adj(g, new_right, left)
    target = column_optimal_swap(m)
    for i, j in target.items():
        g.set_qubit(new_right[i],qs[j])
    new_right.sort(key=g.qubit)
    m = bi_adj(g, new_right, left)
    gates = m.to_cnots(optimize=True)
    for cnot in gates:
        cnot.target = qs[cnot.target]
        cnot.control = qs[cnot.control]
    for i in range(q):
        for j in range(q):
            if g.connected(left[i],new_right[j]):
                if i != j:
                    g.remove_edge(g.edge(left[i],new_right[j]))
            elif i == j:
                g.add_edge((left[i],new_right[j]), EdgeType.HADAMARD)
    if not quiet: print("Greedy extract with {:d} nodes and {:d} CNOTs".format(len(good_nodes),len(gates)))
    return gates, True



def handle_phase_gadget(g, left, neigh, special_nodes, quiet=True):
    """Tries to find a cut of the graph at the given leftrow so that a single phase-gadget can be extracted.
    Returns a list of extracted gates and modifies the graph g in place. Used by :func:`streaming_extract`"""
    q = len(left)
    qs = g.qubits() # We are assuming this thing automatically updates
    rs = g.rows()
    leftrow = rs[left[0]]
    gadgets = neigh.intersection(special_nodes) # These are the phase gadgets that are attached to the left row
    if len(gadgets) == 0: raise ValueError("No phase gadget connected to this row")
    all_verts = neigh.union(left).union(special_nodes.values())
    right = list(neigh)
    options = []
    for gadget in gadgets:
        if all(w in all_verts for w in g.neighbours(gadget)):
            options.append(gadget)
    #print(options)
    for o in options: # We move the candidates gadgets to the end of the list
        right.remove(o)
        right.append(o)
    #print(right)
    m = bi_adj(g, right, left+options)
    r = reduce_bottom_rows(m, q)
    gadget = options[r-len(left)] # This is a gadget that works
    right.remove(gadget)

    g.set_row(gadget,leftrow+1)
    g.set_row(special_nodes[gadget],leftrow+1)

    # Take care nothing is connected directly to an output
    for i in range(len(right)):
        w = right[i]
        if w in g.outputs:
            w2 = g.add_vertex(VertexType.Z, qs[w], rs[w]-1)
            n = list(g.neighbours(w))[0] # Outputs should have unique neighbours
            e = g.edge(n,w)
            et = g.edge_type(e)
            g.remove_edge(e)
            g.add_edge((n,w2),EdgeType.HADAMARD)
            g.add_edge((w2,w),toggle_edge(et))
            right[i] = w2

    if len(right) == q:
        if not quiet: print("No cutting necessary")
        for w in right:
            g.set_row(w, leftrow+2)
    else:
        right = cut_edges(g, left+[gadget], right)
    # We have now prepared the stage to do the extraction of the phase gadget
    
    phase = g.phase(special_nodes[gadget])
    phase = -1*phase if g.phase(gadget) != 0 else phase
    left.sort(key=g.qubit)
    qv = [qs[v] for v in left]
    m = bi_adj(g, right, left)
    target = column_optimal_swap(m)
    for i, j in target.items():
        g.set_qubit(right[i],qv[j])
    right.sort(key=g.qubit)

    m = bi_adj(g, right, left)
    if m.rank() != q:
        raise Exception("Rank in phase gadget reduction too low.")
    operations = Circuit(q)
    operations.row_add = lambda r1,r2: operations.gates.append((r1,r2))
    m.gauss(full_reduce=True,x=operations)
    gates = [CNOT(qv[r2],qv[r1]) for r1,r2 in operations.gates]
    m = bi_adj(g, right+[gadget], left)
    for r1,r2 in operations.gates:
        m.row_add(r1,r2)
    connectivity_from_biadj(g, m, right+[gadget], left)

    # Now the connections from the left to the right are like the identity
    # with some wires coming to the gadget from the left and from the right
    gadget_left = [v for v in left if g.connected(gadget, v)]
    gadget_right = [w for w in right if g.connected(gadget, w)]
    targets = [qs[v] for v in gadget_left]
    # We bring as many connections on the right to the left
    for i in reversed(range(len(gadget_right))): # The following checks if every phase connected node is on the right
        w = gadget_right[i]
        v = next(v for v in left if g.connected(w,v))
        g.set_edge_type((v,w),EdgeType.SIMPLE)
        g.set_qubit(w, qs[v])
        if qs[w] not in targets:
            gates.append(HAD(qs[w]))
            gadget_right.pop(i)
            targets.append(qs[w])
            gadget_left.append(v)
        else:
            g.set_row(w, leftrow+1)

    if not gadget_right: #Only connected on leftside so we are done
        if not quiet: print("Simple phase gadget")
        gate = ParityPhase(phase, *targets)
        g.remove_vertices([special_nodes[gadget],gadget])
        gates.append(gate)
        return gates, leftrow
    
    if not quiet: print("Complicated phase gadget") # targets on left and right, so need to do more
    if len(gadget_right) % 2 != 0 or len(gadget_left) == 1:
        raise Exception("Gadget seems non-unitary")
    
    #Now we can finally extract the phase gadget
    rtargets = []
    for w in gadget_right: 
        t = qs[w]
        rtargets.append(t)
        gates.extend([HAD(t),ZPhase(t,Fraction(-1,2)),HAD(t)])
    if len(gadget_right)%4 != 0: # This is either 2 or 0
        phase = (-phase)%2
    gates.append(ParityPhase(phase, *targets))
    for t in rtargets:
        gates.extend([HAD(t),ZPhase(t, Fraction(1,2))])
    for v in left:
        if qs[v] not in rtargets:
            g.set_row(v, leftrow+1)

    g.remove_vertices([special_nodes[gadget],gadget])
    return gates, leftrow+1

def reduce_bottom_rows(m, qubits):
    """Using just row_add's from the first qubit rows in m, tries to find a row that can be 
    completely zero'd out. Returns the rownumber of this row when successful."""
    cols = m.cols()
    leading_one = {}
    adds = []
    for r in range(qubits):
        while True:
            i = next(i for i in range(cols) if m.data[r][i])
            if i in leading_one:
                m.row_add(leading_one[i],r)
                adds.append((leading_one[i],r))
            else:
                leading_one[i] = r
                break
    for r in range(qubits, m.rows()):
        while True:
            if not any(m.data[r]): 
                return r
            i = next(i for i in range(cols) if m.data[r][i])
            if i not in leading_one: break
            m.row_add(leading_one[i], r)
            adds.append((leading_one[i],r))
    raise ValueError("Did not find any completely reducable row")

def find_ancilla_qubits(g, left, right, gadgets, maxq, quiet=True):
    leftrow = g.row(left[0])
    nodes = list(right.difference(gadgets))
    right = list(right)
    for w in nodes:
        right.remove(w)
        right.append(w)
    m = bi_adj(g, right, left)
    m.gauss(full_reduce=True)
    candidates = []
    ancilla_count = 100000
    for row in m.data:
        if not any(row[:-len(nodes)]):
            verts = [right[i] for i,a in enumerate(row) if a]
            if len(verts) < ancilla_count:
                candidates = [verts]
                ancilla_count = len(verts)
            elif len(verts) == ancilla_count:
                candidates.append(verts)
    if not candidates:
        raise ValueError("No valid ancilla vertices found")
    if not quiet: print("Adding {:d} ancillas".format(ancilla_count-1))
    if len(candidates) == 1:
        ancillas = candidates[0][:-1]
    else:
        all_candidates = set()
        for cand in candidates: all_candidates.update(cand)
        best_set = None
        best_count = 100000
        for poss in itertools.combinations(all_candidates, ancilla_count-1):
            s = sum(1 for cand in candidates if all(v in cand for v in poss))
            if s < best_count:
                best_count = s
                best_set = poss
        ancillas = best_set

    gates = []
    for i, v in enumerate(ancillas):
        g.set_row(v, leftrow)
        g.set_qubit(v, maxq+i)
        w = g.add_vertex(VertexType.Z, maxq+i, leftrow-1)
        g.add_edge((v,w),EdgeType.SIMPLE)
        gates.append(InitAncilla(maxq+i))
    #raise Exception
    return gates, maxq+len(ancillas)




def permutation_as_swaps(perm):
    """Returns a series of swaps the realises the given permutation. 
    Permutation should be a dictionary with both keys and values taking values in 0,1,...,n."""
    swaps = []
    l = [perm[i] for i in range(len(perm))]
    pinv = {v:k for k,v in perm.items()}
    linv = [pinv[i] for i in range(len(pinv))]
    for i in range(len(perm)):
        if l[i] == i: continue
        t1 = l[i]
        t2 = linv[i]
        swaps.append((i,t2))
        #l[i] = i
        #linv[i] = i
        l[t2] = t1
        linv[t1] = t2
    return swaps



# O(N^3)
def max_overlap(cz_matrix):
    """Given an adjacency matrix of qubit connectivity of a CZ circuit, returns:
    a) the rows which have the maximum inner product
    b) the list of common qubits between these rows
    """
    N = len(cz_matrix.data[0])

    max_inner_product = 0
    final_common_qbs = list()
    overlapping_rows = tuple()
    for i in range(N):
        for j in range(i+1,N):
            inner_product = 0
            i_czs = 0
            j_czs = 0
            common_qbs = list()
            for k in range(N):
                i_czs += cz_matrix.data[i][k]
                j_czs += cz_matrix.data[j][k]

                if cz_matrix.data[i][k]!=0 and cz_matrix.data[j][k]!=0:
                    inner_product+=1
                    common_qbs.append(k)

            if inner_product > max_inner_product:
                max_inner_product = inner_product
                if i_czs < j_czs:
                    overlapping_rows = [j,i]
                else:
                    overlapping_rows = [i,j]
                final_common_qbs = common_qbs
    return [overlapping_rows,final_common_qbs]

## Currently broken!!
def modified_extract(g, optimize_czs=True, quiet=True):
    """Given a graph put into semi-normal form by :func:`simplify.full_reduce`, 
    it extracts its equivalent set of gates into an instance of :class:`circuit.Circuit`.
    """
    #g.normalise()
    qs = g.qubits() # We are assuming that these are objects that update...
    rs = g.rows()   # ...to reflect changes to the graph, so that when...
    ty = g.types()  # ... g.set_row/g.set_qubit is called, these things update directly to reflect that
    phases = g.phases()
    c = Circuit(g.qubit_count())

    gadgets = {}
    for v in g.vertices():
        if g.vertex_degree(v) == 1 and v not in g.inputs and v not in g.outputs:
            n = list(g.neighbours(v))[0]
            gadgets[n] = v
    
    qubit_map = dict()
    frontier = []
    for o in g.outputs:
        v = list(g.neighbours(o))[0]
        if v in g.inputs: continue
        frontier.append(v)
        qubit_map[v] = qs[o]
    
    while True:
        # preprocessing
        for v in frontier:
            q = qubit_map[v]
            b = [w for w in g.neighbours(v) if w in g.outputs][0]
            e = g.edge(v,b)
            if g.edge_type(e) == EdgeType.HADAMARD:
                c.add_gate("HAD",q)
                g.set_edge_type(e,EdgeType.SIMPLE)
            if phases[v]: 
                c.add_gate("ZPhase", q, phases[v])
                g.set_phase(v,0)
        cz_mat = Mat2([[0 for i in range(g.qubit_count())] for j in range(g.qubit_count())])
        for v in frontier:
            for w in list(g.neighbours(v)):
                if w in frontier:
                    cz_mat.data[qubit_map[v]][qubit_map[w]] = 1
                    cz_mat.data[qubit_map[w]][qubit_map[v]] = 1
                    g.remove_edge(g.edge(v,w))

        if optimize_czs:
            overlap_data = max_overlap(cz_mat)
            while len(overlap_data[1]) > 2: #there are enough common qubits to be worth optimising
                i,j = overlap_data[0][0], overlap_data[0][1]
                c.add_gate("CNOT",i,j)
                for qb in overlap_data[1]:
                    c.add_gate("CZ",j,qb)
                    cz_mat.data[i][qb]=0
                    cz_mat.data[j][qb]=0
                    cz_mat.data[qb][i]=0
                    cz_mat.data[qb][j]=0
                c.add_gate("CNOT",i,j)
                overlap_data = max_overlap(cz_mat)

        for i in range(g.qubit_count()):
            for j in range(i+1,g.qubit_count()):
                if cz_mat.data[i][j]==1:
                    c.add_gate("CZ",i,j)

        # Check for connectivity to inputs
        neighbours = set()
        for v in frontier.copy():
            d = [w for w in g.neighbours(v) if w not in g.outputs]
            if any(w in g.inputs for w in d):
                if len(d) == 1: # Only connected to input, remove from frontier
                    frontier.remove(v)
                    continue
                # We disconnect v from the input b via a new spider
                b = [w for w in d if w in g.inputs][0]
                q = qs[b]
                r = rs[b]
                w = g.add_vertex(VertexType.Z,q,r+1)
                e = g.edge(v,b)
                et = g.edge_type(e)
                g.remove_edge(e)
                g.add_edge((v,w),EdgeType.SIMPLE)
                g.add_edge((w,b),toggle_edge(et))
                d.remove(b)
                d.append(w)
            neighbours.update(d)
        if not frontier: break # We are done
            
        neighbours = list(neighbours)
        m = bi_adj(g,neighbours,frontier)
        m.gauss(full_reduce=True)
        max_vertices = []
        for l in m.data:
            if sum(l) == 1: 
                i = [i for i,j in enumerate(l) if j == 1][0]
                max_vertices.append(neighbours[i])
        if max_vertices:
            if not quiet: print("Reducing", len(max_vertices), "vertices")
            for v in max_vertices: neighbours.remove(v)
            neighbours = max_vertices + neighbours
            m = bi_adj(g,neighbours,frontier)
            cnots = m.to_cnots()
            for cnot in cnots:
                m.row_add(cnot.target,cnot.control)
                c.add_gate("CNOT",qubit_map[frontier[cnot.control]],qubit_map[frontier[cnot.target]])
            connectivity_from_biadj(g,m,neighbours,frontier)
            good_verts = dict()
            for i, row in enumerate(m.data):
                if sum(row) == 1:
                    v = frontier[i]
                    w = neighbours[[j for j in range(len(row)) if row[j]][0]]
                    good_verts[v] = w
            for v,w in good_verts.items():
                c.add_gate("HAD",qubit_map[v])
                qubit_map[w] = qubit_map[v]
                b = [o for o in g.neighbours(v) if o in g.outputs][0]
                g.remove_vertex(v)
                g.add_edge((w,b))
                frontier.remove(v)
                frontier.append(w)
            if not quiet: print("Vertices extracted:", len(good_verts))
            continue
        else:
            if not quiet: print("No maximal vertex found. Pivoting on gadgets")
            if not quiet: print("Gadgets before:", len(gadgets))
            for w in neighbours:
                if w not in gadgets: continue
                for v in g.neighbours(w):
                    if v in frontier:
                        apply_rule(g,pivot,[(w,v,[],[o for o in g.neighbours(v) if o in g.outputs])])
                        frontier.remove(v)
                        del gadgets[w]
                        frontier.append(w)
                        qubit_map[w] = qubit_map[v]
                        break
            if not quiet: print("Gadgets after:", len(gadgets))
            continue
            
    # Outside of loop. Finish up the permutation
    id_simp(g,quiet=True) # Now the graph should only contain inputs and outputs
    swap_map = {}
    leftover_swaps = False
    for v in g.outputs: # Finally, check for the last layer of Hadamards, and see if swap gates need to be applied.
        q = qs[v]
        i = list(g.neighbours(v))[0]
        if i not in g.inputs: 
            raise TypeError("Algorithm failed: Not fully reducable")
            return c
        if g.edge_type(g.edge(v,i)) == EdgeType.HADAMARD:
            c.add_gate("HAD", q)
            g.set_edge_type(g.edge(v,i),EdgeType.SIMPLE)
        if qs[i] != q: leftover_swaps = True
        swap_map[q] = qs[i]
    if leftover_swaps: 
        for t1, t2 in permutation_as_swaps(swap_map):
            c.add_gate("SWAP", t1, t2)
    # Since we were extracting from right to left, we reverse the order of the gates
    c.gates = list(reversed(c.gates))
    return c